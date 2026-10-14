"""A small in-process imitation of the cTrader Open API server.

Speaks the real Protobuf framing and message types so the client, broker and live
runner can be tested end to end without credentials or network access. It keeps one
hedged USD account with XAUUSD, fills market orders at the current quote, triggers stop
orders and SL/TP on pushed quotes, and serves generated trendbars and ticks.

Run standalone for UI testing: ``python -m gse mock-server``.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time

from . import protocol as P
from .proto import common, messages as M, model

SYMBOL_ID = 41
ACCOUNT_ID = 1234567
MD = 2  # money digits


class MockOpenApiServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, balance: float = 100_000.0,
                 start_price: float = 2400.0, client_id: str = "id", client_secret: str = "secret",
                 access_token: str = "token"):
        self.host, self.port = host, port
        self.balance = balance
        self.bid = start_price
        self.ask = start_price + 0.25
        self.client_id, self.client_secret, self.access_token = client_id, client_secret, access_token
        self.server: asyncio.base_events.Server | None = None
        self.writers: list[asyncio.StreamWriter] = []
        self.orders: dict[int, dict] = {}
        self.positions: dict[int, dict] = {}
        self.ids = itertools.count(1000)
        self.received: list[str] = []
        self.spot_subscribed = False

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    # ------------------------------------------------------------------ plumbing
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        try:
            while True:
                size = int.from_bytes(await reader.readexactly(4), "big")
                pt, msg, cid = P.decode(await reader.readexactly(size))
                self.received.append(P.message_name(pt))
                await self._on_message(writer, pt, msg, cid)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if writer in self.writers:
                self.writers.remove(writer)

    async def _send(self, writer, msg, cid=None) -> None:
        writer.write(P.encode(msg, cid))
        await writer.drain()

    async def _broadcast(self, msg) -> None:
        for w in list(self.writers):
            try:
                await self._send(w, msg)
            except ConnectionError:
                pass

    def _error(self, code: str, desc: str = ""):
        return M.ProtoOAErrorRes(errorCode=code, description=desc, ctidTraderAccountId=ACCOUNT_ID)

    # ------------------------------------------------------------------ requests
    async def _on_message(self, w, pt, msg, cid) -> None:
        A = ACCOUNT_ID
        if pt == common.ProtoHeartbeatEvent().payloadType:
            return
        if pt == model.PROTO_OA_APPLICATION_AUTH_REQ:
            if msg.clientId != self.client_id or msg.clientSecret != self.client_secret:
                return await self._send(w, self._error("CH_CLIENT_AUTH_FAILURE", "bad app credentials"), cid)
            return await self._send(w, M.ProtoOAApplicationAuthRes(), cid)
        if pt == model.PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ:
            res = M.ProtoOAGetAccountListByAccessTokenRes(accessToken=msg.accessToken)
            res.ctidTraderAccount.add(ctidTraderAccountId=A, isLive=False, traderLogin=5100123)
            return await self._send(w, res, cid)
        if pt == model.PROTO_OA_ACCOUNT_AUTH_REQ:
            if msg.accessToken != self.access_token:
                return await self._send(w, self._error("CH_ACCESS_TOKEN_INVALID"), cid)
            return await self._send(w, M.ProtoOAAccountAuthRes(ctidTraderAccountId=A), cid)
        if pt == model.PROTO_OA_TRADER_REQ:
            res = M.ProtoOATraderRes(ctidTraderAccountId=A)
            t = res.trader
            t.ctidTraderAccountId = A
            t.balance = int(round(self.balance * 10 ** MD))
            t.depositAssetId = 1
            t.moneyDigits = MD
            t.brokerName = "Mock Broker"
            t.traderLogin = 5100123
            t.leverageInCents = 3000
            t.accountType = model.HEDGED
            return await self._send(w, res, cid)
        if pt == model.PROTO_OA_ASSET_LIST_REQ:
            res = M.ProtoOAAssetListRes(ctidTraderAccountId=A)
            res.asset.add(assetId=1, name="USD", digits=2)
            res.asset.add(assetId=2, name="XAU", digits=2)
            return await self._send(w, res, cid)
        if pt == model.PROTO_OA_SYMBOLS_LIST_REQ:
            res = M.ProtoOASymbolsListRes(ctidTraderAccountId=A)
            res.symbol.add(symbolId=1, symbolName="EURUSD", enabled=True, baseAssetId=3, quoteAssetId=1)
            res.symbol.add(symbolId=SYMBOL_ID, symbolName="XAUUSD", enabled=True, baseAssetId=2, quoteAssetId=1)
            return await self._send(w, res, cid)
        if pt == model.PROTO_OA_SYMBOL_BY_ID_REQ:
            res = M.ProtoOASymbolByIdRes(ctidTraderAccountId=A)
            s = res.symbol.add(symbolId=SYMBOL_ID, digits=2, pipPosition=1)
            s.lotSize = 10000
            s.minVolume = 100
            s.stepVolume = 100
            s.maxVolume = 10_000_000
            s.commissionType = model.USD_PER_LOT
            s.preciseTradingCommissionRate = int(3.5 * 1e8)
            return await self._send(w, res, cid)
        if pt == model.PROTO_OA_SYMBOLS_FOR_CONVERSION_REQ:
            return await self._send(w, M.ProtoOASymbolsForConversionRes(ctidTraderAccountId=A), cid)
        if pt == model.PROTO_OA_RECONCILE_REQ:
            res = M.ProtoOAReconcileRes(ctidTraderAccountId=A)
            for p in self.positions.values():
                res.position.append(self._position_msg(p))
            for o in self.orders.values():
                res.order.append(self._order_msg(o))
            return await self._send(w, res, cid)
        if pt == model.PROTO_OA_SUBSCRIBE_SPOTS_REQ:
            self.spot_subscribed = True
            return await self._send(w, M.ProtoOASubscribeSpotsRes(ctidTraderAccountId=A), cid)
        if pt == model.PROTO_OA_SUBSCRIBE_LIVE_TRENDBAR_REQ:
            return await self._send(w, M.ProtoOASubscribeLiveTrendbarRes(ctidTraderAccountId=A), cid)
        if pt == model.PROTO_OA_GET_TRENDBARS_REQ:
            return await self._send(w, self._trendbars(msg), cid)
        if pt == model.PROTO_OA_GET_TICKDATA_REQ:
            return await self._send(w, self._ticks(msg), cid)
        if pt == model.PROTO_OA_ACCOUNT_LOGOUT_REQ:
            return await self._send(w, M.ProtoOAAccountLogoutRes(ctidTraderAccountId=A), cid)
        if pt == model.PROTO_OA_DEAL_LIST_REQ:
            return await self._send(w, M.ProtoOADealListRes(ctidTraderAccountId=A, hasMore=False), cid)
        if pt == model.PROTO_OA_NEW_ORDER_REQ:
            return await self._new_order(w, msg, cid)
        if pt == model.PROTO_OA_CANCEL_ORDER_REQ:
            o = self.orders.pop(msg.orderId, None)
            if o is None:
                return await self._send(w, self._error("ORDER_NOT_FOUND"), cid)
            o["status"] = model.ORDER_STATUS_CANCELLED
            return await self._send(w, self._exec(model.ORDER_CANCELLED, order=o), cid)
        if pt == model.PROTO_OA_AMEND_ORDER_REQ:
            o = self.orders.get(msg.orderId)
            if o is None:
                return await self._send(w, self._error("ORDER_NOT_FOUND"), cid)
            for f in ("stopPrice", "stopLoss", "takeProfit", "volume", "expirationTimestamp"):
                if msg.HasField(f):
                    o[f] = getattr(msg, f)
            return await self._send(w, self._exec(model.ORDER_REPLACED, order=o), cid)
        if pt == model.PROTO_OA_AMEND_POSITION_SLTP_REQ:
            p = self.positions.get(msg.positionId)
            if p is None:
                return await self._send(w, self._error("POSITION_NOT_FOUND"), cid)
            p["stopLoss"] = msg.stopLoss if msg.HasField("stopLoss") else None
            p["takeProfit"] = msg.takeProfit if msg.HasField("takeProfit") else None
            return await self._send(w, self._exec(model.ORDER_REPLACED, position=p), cid)
        if pt == model.PROTO_OA_CLOSE_POSITION_REQ:
            p = self.positions.get(msg.positionId)
            if p is None:
                return await self._send(w, self._error("POSITION_NOT_FOUND"), cid)
            price = self.bid if p["side"] == model.BUY else self.ask
            return await self._close(p, msg.volume, price, model.MARKET, w, cid)
        await self._send(w, self._error("UNSUPPORTED_MESSAGE", P.message_name(pt)), cid)

    # ------------------------------------------------------------------ trading
    async def _new_order(self, w, msg, cid) -> None:
        oid = next(self.ids)
        o = {
            "orderId": oid, "side": msg.tradeSide, "volume": msg.volume, "type": msg.orderType,
            "label": msg.label, "comment": msg.comment, "clientOrderId": msg.clientOrderId,
            "stopPrice": msg.stopPrice if msg.HasField("stopPrice") else None,
            "stopLoss": msg.stopLoss if msg.HasField("stopLoss") else None,
            "takeProfit": msg.takeProfit if msg.HasField("takeProfit") else None,
            "relSL": msg.relativeStopLoss if msg.HasField("relativeStopLoss") else None,
            "relTP": msg.relativeTakeProfit if msg.HasField("relativeTakeProfit") else None,
            "expirationTimestamp": msg.expirationTimestamp if msg.HasField("expirationTimestamp") else None,
            "status": model.ORDER_STATUS_ACCEPTED,
        }
        if msg.volume < 100 or msg.volume % 100:
            return await self._send(w, M.ProtoOAOrderErrorEvent(ctidTraderAccountId=ACCOUNT_ID,
                                                                 errorCode="TRADING_BAD_VOLUME"), cid)
        if msg.orderType == model.MARKET:
            await self._send(w, self._exec(model.ORDER_ACCEPTED, order=o), cid)
            price = self.ask if o["side"] == model.BUY else self.bid
            await self._fill_open(o, price)
        else:
            self.orders[oid] = o
            await self._send(w, self._exec(model.ORDER_ACCEPTED, order=o), cid)

    async def _fill_open(self, o: dict, price: float) -> None:
        pid = next(self.ids)
        sign = 1 if o["side"] == model.BUY else -1
        sl = o["stopLoss"]
        tp = o["takeProfit"]
        if o.get("relSL"):
            sl = round(price - sign * o["relSL"] / P.PRICE_SCALE, 2)
        if o.get("relTP"):
            tp = round(price + sign * o["relTP"] / P.PRICE_SCALE, 2)
        comm = 3.5 * (o["volume"] / 10000)
        self.balance -= comm
        p = {"positionId": pid, "side": o["side"], "volume": o["volume"], "price": price, "stopLoss": sl,
             "takeProfit": tp, "label": o["label"], "comment": o["comment"], "open_ts": int(time.time() * 1000),
             "status": model.POSITION_STATUS_OPEN}
        self.positions[pid] = p
        self.orders.pop(o["orderId"], None)
        o["status"] = model.ORDER_STATUS_FILLED
        deal = {"price": price, "volume": o["volume"], "commission": -int(round(comm * 10 ** MD)), "close": None}
        await self._broadcast(self._exec(model.ORDER_FILLED, order=o, position=p, deal=deal))

    async def _close(self, p: dict, volume: int, price: float, order_type: int, w=None, cid=None) -> None:
        volume = min(volume, p["volume"])
        sign = 1 if p["side"] == model.BUY else -1
        gross = (price - p["price"]) * sign * volume / 100
        comm = 3.5 * (volume / 10000)
        self.balance += gross - comm
        p["volume"] -= volume
        if p["volume"] <= 0:
            p["status"] = model.POSITION_STATUS_CLOSED
            self.positions.pop(p["positionId"], None)
        o = {"orderId": next(self.ids), "side": model.SELL if sign > 0 else model.BUY, "volume": volume,
             "type": order_type, "label": p["label"], "comment": p["comment"], "clientOrderId": None,
             "stopPrice": None, "stopLoss": None, "takeProfit": None, "status": model.ORDER_STATUS_FILLED,
             "positionId": p["positionId"]}
        deal = {"price": price, "volume": volume, "commission": 0,
                "close": {"entry": p["price"], "gross": int(round(gross * 10 ** MD)), "comm": -int(round(comm * 10 ** MD)),
                          "balance": int(round(self.balance * 10 ** MD)), "closed": volume}}
        ev = self._exec(model.ORDER_FILLED, order=o, position=p, deal=deal)
        if w is not None:
            await self._send(w, ev, cid)
        else:
            await self._broadcast(ev)

    async def push_quote(self, bid: float, ask: float | None = None) -> None:
        """Move the market: triggers stop orders and SL/TP, then broadcasts a spot event."""
        self.bid = round(bid, 2)
        self.ask = round(ask if ask is not None else bid + 0.25, 2)
        for o in list(self.orders.values()):
            if o["type"] != model.STOP:
                continue
            if (o["side"] == model.BUY and self.ask >= o["stopPrice"]) or (o["side"] == model.SELL and self.bid <= o["stopPrice"]):
                await self._fill_open(o, self.ask if o["side"] == model.BUY else self.bid)
        for p in list(self.positions.values()):
            long = p["side"] == model.BUY
            mark = self.bid if long else self.ask
            sl, tp = p.get("stopLoss"), p.get("takeProfit")
            if sl is not None and ((long and mark <= sl) or (not long and mark >= sl)):
                await self._close(p, p["volume"], mark, model.STOP_LOSS_TAKE_PROFIT)
            elif tp is not None and ((long and mark >= tp) or (not long and mark <= tp)):
                await self._close(p, p["volume"], mark, model.STOP_LOSS_TAKE_PROFIT)
        if self.spot_subscribed:
            ev = M.ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT_ID, symbolId=SYMBOL_ID,
                                    bid=int(round(self.bid * P.PRICE_SCALE)), ask=int(round(self.ask * P.PRICE_SCALE)),
                                    timestamp=int(time.time() * 1000))
            await self._broadcast(ev)

    # ------------------------------------------------------------------ message builders
    def _trade_data(self, td, side, volume, label, comment):
        td.symbolId = SYMBOL_ID
        td.volume = volume
        td.tradeSide = side
        td.label = label or ""
        td.comment = comment or ""

    def _order_msg(self, o: dict):
        msg = model.ProtoOAOrder(orderId=o["orderId"], orderType=o["type"], orderStatus=o["status"])
        self._trade_data(msg.tradeData, o["side"], o["volume"], o["label"], o["comment"])
        if o.get("clientOrderId"):
            msg.clientOrderId = o["clientOrderId"]
        for f in ("stopPrice", "stopLoss", "takeProfit"):
            if o.get(f) is not None:
                setattr(msg, f, o[f])
        if o.get("positionId"):
            msg.positionId = o["positionId"]
            msg.closingOrder = True
        return msg

    def _position_msg(self, p: dict):
        msg = model.ProtoOAPosition(positionId=p["positionId"], positionStatus=p["status"], swap=0, price=p["price"],
                                 moneyDigits=MD)
        self._trade_data(msg.tradeData, p["side"], p["volume"], p["label"], p["comment"])
        msg.tradeData.openTimestamp = p["open_ts"]
        if p.get("stopLoss") is not None:
            msg.stopLoss = p["stopLoss"]
        if p.get("takeProfit") is not None:
            msg.takeProfit = p["takeProfit"]
        return msg

    def _exec(self, et, order=None, position=None, deal=None):
        ev = M.ProtoOAExecutionEvent(ctidTraderAccountId=ACCOUNT_ID, executionType=et)
        if order is not None:
            ev.order.CopyFrom(self._order_msg(order))
        if position is not None:
            ev.position.CopyFrom(self._position_msg(position))
        if deal is not None:
            d = ev.deal
            d.dealId = next(self.ids)
            d.orderId = order["orderId"] if order else 0
            d.positionId = position["positionId"] if position else 0
            d.volume = d.filledVolume = deal["volume"]
            d.symbolId = SYMBOL_ID
            d.createTimestamp = d.executionTimestamp = int(time.time() * 1000)
            d.executionPrice = deal["price"]
            d.tradeSide = order["side"] if order else model.BUY
            d.dealStatus = model.FILLED
            d.commission = deal["commission"]
            d.moneyDigits = MD
            if deal.get("close"):
                c = deal["close"]
                cp = d.closePositionDetail
                cp.entryPrice = c["entry"]
                cp.grossProfit = c["gross"]
                cp.swap = 0
                cp.commission = c["comm"]
                cp.balance = c["balance"]
                cp.closedVolume = c["closed"]
                cp.moneyDigits = MD
        return ev

    def _trendbars(self, msg):
        res = M.ProtoOAGetTrendbarsRes(ctidTraderAccountId=ACCOUNT_ID, period=msg.period, timestamp=msg.toTimestamp,
                                       symbolId=msg.symbolId)
        secs = P.PERIOD_SECONDS.get(msg.period, 60)
        start = msg.fromTimestamp // 1000 // secs * secs
        end = msg.toTimestamp // 1000
        t = start
        while t < end:
            k = t // secs
            dow = (t // 86400 + 4) % 7
            if dow != 5:
                base = 2400 + 15 * math.sin(k / 97.0) + 4 * math.sin(k / 7.3)
                o, c = base, base + 0.4 * math.sin(k / 3.1)
                h, l = max(o, c) + 0.6, min(o, c) - 0.6
                low = int(round(l * P.PRICE_SCALE))
                res.trendbar.add(volume=50, period=msg.period, low=low,
                                 deltaOpen=int(round(o * P.PRICE_SCALE)) - low,
                                 deltaClose=int(round(c * P.PRICE_SCALE)) - low,
                                 deltaHigh=int(round(h * P.PRICE_SCALE)) - low,
                                 utcTimestampInMinutes=t // 60)
            t += secs
        return res

    def _ticks(self, msg):
        """Ticks every 10 seconds, newest first, delta-encoded, at most 500 per page."""
        res = M.ProtoOAGetTickDataRes(ctidTraderAccountId=ACCOUNT_ID, hasMore=False)
        start = (msg.fromTimestamp + 9_999) // 10_000 * 10_000
        stamps = list(range(msg.toTimestamp // 10_000 * 10_000, start - 1, -10_000))
        if len(stamps) > 500:
            stamps = stamps[:500]
            res.hasMore = True
        prev_ts = prev_px = None
        for ts in stamps:
            px = 2400 + 10 * math.sin(ts / 3.6e6) + (0.3 if msg.type == model.ASK else 0.0)
            px_i = int(round(px * 100)) * 1000
            if prev_ts is None:
                res.tickData.add(timestamp=ts, tick=px_i)
            else:
                res.tickData.add(timestamp=ts - prev_ts, tick=px_i - prev_px)
            prev_ts, prev_px = ts, px_i
        return res


async def serve_forever(port: int = 5035) -> None:  # pragma: no cover - manual tool
    srv = MockOpenApiServer(port=port)
    await srv.start()
    print(f"Mock cTrader Open API on 127.0.0.1:{srv.port} (client id 'id', secret 'secret', token 'token')")
    price = srv.bid
    k = 0
    while True:
        await asyncio.sleep(1)
        k += 1
        price += 0.4 * math.sin(k / 9) + 0.2 * math.sin(k / 2.3)
        await srv.push_quote(price)
