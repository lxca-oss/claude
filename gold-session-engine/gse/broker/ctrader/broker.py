"""Live order execution on a cTrader account through the Open API.

* Every order carries ``label`` (default "GSE") and the engine's ``ref`` as
  ``clientOrderId`` and comment, so after a restart the bot recognises its own positions
  and never touches manual trades or other bots.
* Stop entries are real STOP orders on the server with absolute SL/TP and a
  good-till-date expiry, so they are protected even if the bot disconnects.
* Market entries go out with relative SL/TP (the only form cTrader accepts on market
  orders) and are amended to the exact planned levels once filled.
* Break-even moves and partial targets are watched on every quote and sent as
  amend/close requests. The stop loss always sits on the server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Callable

from ...models import Order, Position, SymbolSpec
from ..base import Broker, make_closed_trade
from . import protocol as P
from .client import OpenApiError
from .proto import messages as M
from .proto import model
from .session import CTraderSession

log = logging.getLogger(__name__)

ExecutionType = model.ProtoOAExecutionType


class CTraderBroker(Broker):
    def __init__(self, session: CTraderSession, symbol_id: int, symbol: SymbolSpec, label: str,
                 balance: float, money_digits: int,
                 notify: Callable[[str, str], None] | None = None,
                 trade_meta: Callable[[str], dict | None] | None = None):
        self.session = session
        self.client = session.client
        self.account_id = session.account_id
        self.symbol_id = symbol_id
        self.symbol = symbol
        self.label = label
        self._balance = balance
        self.money_digits = money_digits
        self.notify = notify or (lambda level, msg: None)
        self.trade_meta = trade_meta or (lambda ref: None)
        self.listener = None
        self.bid: float | None = None
        self.ask: float | None = None
        self.now = int(time.time())
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.order_ids: dict[int, str] = {}
        self.position_ids: dict[int, str] = {}
        self._cancel_reasons: dict[str, str] = {}
        self._close_reasons: dict[str, str] = {}
        self._deferred: dict[str, list] = {}
        self._inflight: dict[tuple, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self.recent_errors: deque[dict] = deque(maxlen=50)
        self.client.on(model.PROTO_OA_EXECUTION_EVENT, self._on_execution)
        self.client.on(model.PROTO_OA_ORDER_ERROR_EVENT, self._on_order_error)
        self.client.on(model.PROTO_OA_TRADER_UPDATE_EVENT, self._on_trader_update)

    # ------------------------------------------------------------------ helpers
    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _error(self, msg: str) -> None:
        self.recent_errors.append({"ts": int(time.time()), "message": msg})
        log.warning(msg)
        self.notify("error", msg)

    def _px(self, x: float) -> float:
        return round(x, self.symbol.digits)

    # ------------------------------------------------------------------ account
    def balance(self) -> float:
        return self._balance

    def equity(self) -> float:
        if self.bid is None:
            return self._balance
        q2a = self.symbol.quote_to_account
        return self._balance + sum(p.unrealized_quote(self.bid, self.ask) * q2a for p in self.positions.values())

    def quote(self) -> tuple[float, float]:
        return self.bid, self.ask

    def open_positions(self) -> list[Position]:
        return list(self.positions.values())

    def pending_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.kind == "stop"]

    async def refresh_balance(self) -> None:
        t = await self.session.trader()
        md = t.moneyDigits if t.HasField("moneyDigits") else self.money_digits
        self.money_digits = md
        self._balance = P.money(t.balance, md)

    # ------------------------------------------------------------------ requests from the engine
    def place(self, order: Order) -> None:
        self.orders[order.ref] = order
        self._spawn(self._send_new(order))

    async def _send_new(self, o: Order) -> None:
        sym = self.symbol
        req = M.ProtoOANewOrderReq(
            ctidTraderAccountId=self.account_id, symbolId=self.symbol_id,
            tradeSide=model.BUY if o.side > 0 else model.SELL,
            volume=P.units_to_volume(o.units), label=self.label, comment=o.ref[:100], clientOrderId=o.ref,
        )
        if o.kind == "market":
            ref_px = self.ask if o.side > 0 else self.bid
            if ref_px is None:
                self._reject_local(o, "no price yet")
                return
            req.orderType = model.MARKET
            req.relativeStopLoss = P.relative_points(abs(ref_px - o.sl), sym.digits)
            req.relativeTakeProfit = P.relative_points(abs(o.tp - ref_px), sym.digits)
        else:
            req.orderType = model.STOP
            req.stopPrice = self._px(o.entry)
            req.stopLoss = self._px(o.sl)
            req.takeProfit = self._px(o.tp)
            if o.expiry_ts:
                req.timeInForce = model.GOOD_TILL_DATE
                req.expirationTimestamp = int(o.expiry_ts * 1000)
            else:
                req.timeInForce = model.GOOD_TILL_CANCEL
        try:
            await self.client.request(req, timeout=20)
        except OpenApiError as exc:
            self._reject_local(o, f"{exc.code} {exc.description}".strip())
        except asyncio.TimeoutError:
            self._error(f"No response to order {o.ref} within 20s; state will be reconciled from events")
        except ConnectionError as exc:
            self._reject_local(o, f"not sent: {exc}")

    def _reject_local(self, o: Order, reason: str) -> None:
        if self.orders.pop(o.ref, None) is not None:
            self._error(f"Order {o.ref} rejected: {reason}")
            if self.listener:
                self.listener.on_order_rejected(o, reason, int(time.time()))

    def amend(self, ref, entry, sl, tp, units, be_trigger=None) -> None:
        o = self.orders.get(ref)
        if o is None:
            return
        o.entry, o.sl, o.tp, o.units, o.be_trigger = entry, sl, tp, units, be_trigger
        if o.broker_id is None:
            self._deferred.setdefault(ref, []).append("amend")
            return
        self._spawn(self._send_amend_order(o))

    async def _send_amend_order(self, o: Order) -> None:
        req = M.ProtoOAAmendOrderReq(
            ctidTraderAccountId=self.account_id, orderId=o.broker_id, volume=P.units_to_volume(o.units),
            stopPrice=self._px(o.entry), stopLoss=self._px(o.sl), takeProfit=self._px(o.tp),
        )
        if o.expiry_ts:
            req.expirationTimestamp = int(o.expiry_ts * 1000)
        try:
            await self.client.request(req)
        except (OpenApiError, asyncio.TimeoutError, ConnectionError) as exc:
            self._error(f"Amend of {o.ref} failed: {exc}")

    def cancel(self, ref: str, reason: str) -> None:
        o = self.orders.get(ref)
        if o is None or ref in self._cancel_reasons:
            return
        self._cancel_reasons[ref] = reason
        if o.broker_id is None:
            self._deferred.setdefault(ref, []).append("cancel")
            return
        self._spawn(self._send_cancel(o))

    async def _send_cancel(self, o: Order) -> None:
        try:
            await self.client.request(M.ProtoOACancelOrderReq(ctidTraderAccountId=self.account_id, orderId=o.broker_id))
        except (OpenApiError, asyncio.TimeoutError, ConnectionError) as exc:
            self._cancel_reasons.pop(o.ref, None)
            self._error(f"Cancel of {o.ref} failed: {exc}")

    def modify_position(self, ref, sl=None, tp=None) -> None:
        p = self.positions.get(ref)
        if p is None:
            return
        self._spawn(self._send_amend_position(p, sl if sl is not None else p.sl, tp if tp is not None else p.tp))

    async def _send_amend_position(self, p: Position, sl: float, tp: float) -> bool:
        try:
            await self.client.request(M.ProtoOAAmendPositionSLTPReq(
                ctidTraderAccountId=self.account_id, positionId=p.broker_id, stopLoss=self._px(sl),
                takeProfit=self._px(tp)))
            p.sl, p.tp = self._px(sl), self._px(tp)
            return True
        except (OpenApiError, asyncio.TimeoutError, ConnectionError) as exc:
            self._error(f"SL/TP amend of {p.ref} failed: {exc}")
            return False

    def close(self, ref: str, reason: str, units: float | None = None) -> None:
        p = self.positions.get(ref)
        if p is None:
            return
        key = ("close", ref, units)
        if key in self._inflight and time.time() - self._inflight[key] < 10:
            return
        self._inflight[key] = time.time()
        if units is None or units >= p.units:
            self._close_reasons[ref] = reason
            units = p.units
        self._spawn(self._send_close(p, units, key))

    async def _send_close(self, p: Position, units: float, key) -> None:
        try:
            await self.client.request(M.ProtoOAClosePositionReq(
                ctidTraderAccountId=self.account_id, positionId=p.broker_id, volume=P.units_to_volume(units)))
        except (OpenApiError, asyncio.TimeoutError, ConnectionError) as exc:
            self._inflight.pop(key, None)
            self._error(f"Close of {p.ref} failed: {exc}")

    # ------------------------------------------------------------------ quotes and position management
    def on_quote(self, ts: int, bid: float | None, ask: float | None) -> None:
        if bid is not None:
            self.bid = bid
        if ask is not None:
            self.ask = ask
        self.now = ts
        if self.bid is None or self.ask is None:
            return
        for ref, p in list(self.positions.items()):
            mark = p.mark(self.bid, self.ask)
            if (mark - p.best) * p.side > 0:
                p.best = mark
            if (mark - p.worst) * p.side < 0:
                p.worst = mark
            if p.partial_price is not None and not p.partial_done and (mark - p.partial_price) * p.side >= 0:
                units = self.symbol.floor_units(p.initial_units * p.partial_fraction)
                if 0 < units < p.units:
                    self.close(ref, "partial target", units)
                else:
                    p.partial_done = True
            if p.be_trigger is not None and not p.be_done and (mark - p.be_trigger) * p.side >= 0:
                key = ("be", ref)
                if key not in self._inflight or time.time() - self._inflight[key] > 10:
                    self._inflight[key] = time.time()
                    self._spawn(self._move_to_breakeven(p))

    async def _move_to_breakeven(self, p: Position) -> None:
        be = p.entry + p.side * p.be_offset
        if await self._send_amend_position(p, be, p.tp):
            p.be_done = True
            if self.listener:
                self.listener.on_position_event(p, "breakeven", {"sl": p.sl}, int(time.time()))

    # ------------------------------------------------------------------ execution events
    def _ref_for(self, order, position) -> str | None:
        if order is not None:
            if order.HasField("clientOrderId") and order.clientOrderId in self.orders:
                return order.clientOrderId
            if order.orderId in self.order_ids:
                return self.order_ids[order.orderId]
        if position is not None:
            if position.positionId in self.position_ids:
                return self.position_ids[position.positionId]
        if order is not None and order.HasField("clientOrderId"):
            return order.clientOrderId
        return None

    def _ours(self, order, position) -> bool:
        if position is not None and position.positionId in self.position_ids:
            return True
        if order is not None and (order.orderId in self.order_ids
                                  or (order.HasField("clientOrderId") and order.clientOrderId in self.orders)):
            return True
        for msg in (position, order):
            if msg is not None and msg.tradeData.symbolId == self.symbol_id and msg.tradeData.label == self.label:
                return True
        return False

    async def _on_execution(self, pt, ev) -> None:
        if ev.ctidTraderAccountId != self.account_id:
            return
        et = ev.executionType
        order = ev.order if ev.HasField("order") else None
        pos = ev.position if ev.HasField("position") else None
        deal = ev.deal if ev.HasField("deal") else None
        ts = int(deal.executionTimestamp / 1000) if deal is not None else int(time.time())

        if et == ExecutionType.Value("DEPOSIT_WITHDRAW") and ev.HasField("depositWithdraw"):
            dw = ev.depositWithdraw
            self._balance = P.money(dw.balance, dw.moneyDigits if dw.HasField("moneyDigits") else self.money_digits)
            self.notify("info", f"Balance changed by deposit/withdrawal: {self._balance:.2f}")
            return
        if not self._ours(order, pos):
            return
        ref = self._ref_for(order, pos)
        if ref is None:
            return

        if et == ExecutionType.Value("ORDER_ACCEPTED"):
            o = self.orders.get(ref)
            if o is not None and order is not None:
                o.broker_id = order.orderId
                self.order_ids[order.orderId] = ref
                for action in self._deferred.pop(ref, []):
                    if action == "cancel":
                        self._spawn(self._send_cancel(o))
                    elif action == "amend":
                        self._spawn(self._send_amend_order(o))
        elif et in (ExecutionType.Value("ORDER_FILLED"), ExecutionType.Value("ORDER_PARTIAL_FILL")):
            if deal is not None and deal.HasField("closePositionDetail"):
                self._on_closing_deal(ref, order, pos, deal, ts)
            else:
                self._on_opening_fill(ref, order, pos, deal, ts)
        elif et in (ExecutionType.Value("ORDER_CANCELLED"), ExecutionType.Value("ORDER_EXPIRED")):
            o = self.orders.pop(ref, None)
            reason = self._cancel_reasons.pop(ref, "expired" if et == ExecutionType.Value("ORDER_EXPIRED") else "cancelled")
            if o is not None and self.listener:
                self.listener.on_order_cancelled(o, reason, ts)
        elif et == ExecutionType.Value("ORDER_REJECTED"):
            o = self.orders.pop(ref, None)
            if o is not None:
                self._error(f"Order {ref} rejected: {ev.errorCode}")
                if self.listener:
                    self.listener.on_order_rejected(o, ev.errorCode or "rejected", ts)
        elif et == ExecutionType.Value("ORDER_REPLACED"):
            p = self.positions.get(ref)
            if p is not None and pos is not None:
                if pos.HasField("stopLoss"):
                    p.sl = pos.stopLoss
                if pos.HasField("takeProfit"):
                    p.tp = pos.takeProfit

    def _on_opening_fill(self, ref, order, pos, deal, ts) -> None:
        o = self.orders.pop(ref, None)
        if o is None or pos is None:
            return
        o.broker_id = order.orderId if order is not None else o.broker_id
        units = P.volume_to_units(pos.tradeData.volume)
        entry = pos.price if pos.HasField("price") else deal.executionPrice
        md = deal.moneyDigits if deal is not None and deal.HasField("moneyDigits") else self.money_digits
        comm = abs(P.money(deal.commission, md)) if deal is not None and deal.HasField("commission") else 0.0
        self._balance -= comm
        p = Position(
            ref=ref, module=o.module, side=o.side, units=units, initial_units=units, entry=entry,
            sl=pos.stopLoss if pos.HasField("stopLoss") else o.sl,
            tp=pos.takeProfit if pos.HasField("takeProfit") else o.tp,
            open_ts=ts, risk_money=o.risk_money, r_dist=o.r_dist, initial_sl=o.sl, planned_entry=o.entry,
            be_trigger=o.be_trigger, be_offset=o.be_offset, partial_price=o.partial_price,
            partial_fraction=o.partial_fraction, realized_pnl=-comm, commission=comm, tag=o.tag,
            notes=list(o.notes), broker_id=pos.positionId,
        )
        self.positions[ref] = p
        self.position_ids[pos.positionId] = ref
        if o.oco_group:
            for other_ref, other in list(self.orders.items()):
                if other.oco_group == o.oco_group:
                    self.cancel(other_ref, "OCO: other side filled")
        if o.kind == "market" and (abs(p.sl - o.sl) > 1e-6 or abs(p.tp - o.tp) > 1e-6):
            self._spawn(self._send_amend_position(p, o.sl, o.tp))
        if self.listener:
            self.listener.on_order_filled(o, p, ts)

    def _on_closing_deal(self, ref, order, pos, deal, ts) -> None:
        p = self.positions.get(ref)
        if p is None:
            return
        cpd = deal.closePositionDetail
        md = cpd.moneyDigits if cpd.HasField("moneyDigits") else self.money_digits
        closed_units = P.volume_to_units(cpd.closedVolume) if cpd.HasField("closedVolume") else P.volume_to_units(deal.filledVolume)
        price = deal.executionPrice
        comm = abs(P.money(cpd.commission, md))
        net = P.money(cpd.grossProfit, md) + P.money(cpd.swap, md) - comm
        if cpd.HasField("pnlConversionFee"):
            net -= abs(P.money(cpd.pnlConversionFee, md))
        if cpd.HasField("balance"):
            self._balance = P.money(cpd.balance, md)
        fully_closed = pos is not None and pos.positionStatus == model.POSITION_STATUS_CLOSED
        if not fully_closed and pos is not None and pos.tradeData.volume == 0:
            fully_closed = True
        for key in [k for k in self._inflight if k[0] == "close" and k[1] == ref]:
            self._inflight.pop(key, None)
        if fully_closed:
            reason = self._close_reasons.pop(ref, None) or self._infer_reason(p, order, price)
            self.positions.pop(ref, None)
            self.position_ids.pop(p.broker_id, None)
            trade = make_closed_trade(p, ts, price, closed_units, reason, net, comm)
            if self.listener:
                self.listener.on_position_closed(p, trade)
        else:
            p.units = round(p.units - closed_units, 8)
            p.realized_pnl += net
            p.commission += comm
            p.exit_value += price * closed_units
            p.exited_units += closed_units
            p.partial_done = True
            if self.listener:
                self.listener.on_position_event(p, "partial", {"price": price, "units": closed_units, "pnl": net}, ts)

    @staticmethod
    def _infer_reason(p: Position, order, price: float) -> str:
        if order is not None and order.HasField("isStopOut") and order.isStopOut:
            return "stop out"
        if order is not None and order.orderType == model.STOP_LOSS_TAKE_PROFIT:
            if abs(price - p.tp) < abs(price - p.sl):
                return "tp"
            if p.be_done and abs(p.sl - (p.entry + p.side * p.be_offset)) < 0.01:
                return "breakeven"
            return "sl"
        return "close"

    def _on_order_error(self, pt, ev) -> None:
        if ev.ctidTraderAccountId != self.account_id:
            return
        self._error(f"Order error {ev.errorCode}: {ev.description}")

    def _on_trader_update(self, pt, ev) -> None:
        if ev.ctidTraderAccountId != self.account_id:
            return
        t = ev.trader
        md = t.moneyDigits if t.HasField("moneyDigits") else self.money_digits
        self._balance = P.money(t.balance, md)

    # ------------------------------------------------------------------ restart recovery
    async def restore(self) -> list[Position]:
        """Adopt this bot's open positions after a restart and cancel its stale pending orders.

        Pending entries are not adopted: the modules re-place them if they are still valid.
        """
        res = await self.session.reconcile()
        restored = []
        for pos in res.position:
            td = pos.tradeData
            if td.symbolId != self.symbol_id or td.label != self.label:
                continue
            ref = td.comment or f"X{pos.positionId}"
            meta = self.trade_meta(ref) or {}
            side = 1 if td.tradeSide == model.BUY else -1
            units = P.volume_to_units(td.volume)
            sl = pos.stopLoss if pos.HasField("stopLoss") else meta.get("sl_initial", pos.price - side * 5)
            tp = pos.takeProfit if pos.HasField("takeProfit") else meta.get("tp", pos.price + side * 10)
            r_dist = meta.get("r_dist") or abs(pos.price - meta.get("sl_initial", sl)) or 1.0
            p = Position(
                ref=ref, module=meta.get("module") or (ref[0] if ref[:1] in "ABCD" else "?"), side=side,
                units=units, initial_units=meta.get("units", units), entry=pos.price, sl=sl, tp=tp,
                open_ts=int(td.openTimestamp / 1000) if td.HasField("openTimestamp") else int(time.time()),
                risk_money=meta.get("risk_money") or units * r_dist, r_dist=r_dist,
                initial_sl=meta.get("sl_initial", sl), planned_entry=meta.get("planned_entry", pos.price),
                be_trigger=meta.get("be_trigger"), partial_price=meta.get("partial_price"),
                partial_fraction=meta.get("partial_fraction") or 0.0,
                partial_done=units < meta.get("units", units),
                be_done=abs(sl - pos.price) < 0.01, tag=meta.get("tag", ""), broker_id=pos.positionId,
            )
            self.positions[ref] = p
            self.position_ids[pos.positionId] = ref
            restored.append(p)
        for o in res.order:
            td = o.tradeData
            if td.symbolId == self.symbol_id and td.label == self.label:
                try:
                    await self.client.request(M.ProtoOACancelOrderReq(ctidTraderAccountId=self.account_id, orderId=o.orderId))
                except (OpenApiError, asyncio.TimeoutError) as exc:
                    self._error(f"Could not cancel stale order {o.orderId}: {exc}")
        return restored
