"""Simulated broker for backtests, replays and paper trading.

Bars are bid prices; the ask is bid + spread. Inside each M1 bar price is assumed to
travel open -> low -> high -> close for an up bar and open -> high -> low -> close for a
down bar, and every stop entry, stop loss, take profit, break-even move and partial
target is triggered in the order price reaches it along that path. When a bar opens
beyond a level (a gap) the fill happens at the open, never at the better level.

Costs: the spread (per bar if the data has it), adverse slippage on stop and market
fills and on stop losses, and a round-turn commission charged half on entry and half
on exit.
"""

from __future__ import annotations

from ..bars import Bar
from ..config import CostSettings
from ..models import Order, Position, SymbolSpec
from .base import Broker, make_closed_trade

UP, DOWN = 1, -1


class SimBroker(Broker):
    def __init__(self, symbol: SymbolSpec, costs: CostSettings, initial_balance: float):
        self.symbol = symbol
        self.costs = costs
        self._balance = float(initial_balance)
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.bid: float | None = None
        self.ask: float | None = None
        self.now = 0
        self.listener = None
        self.fills = 0

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
        return list(self.orders.values())

    # ------------------------------------------------------------------ requests
    def place(self, order: Order) -> None:
        if order.kind == "market":
            if order.side > 0:
                price = self.ask + self.costs.slippage_usd
            else:
                price = self.bid - self.costs.slippage_usd
            self._open(order, price)
        else:
            order.status = "pending"
            self.orders[order.ref] = order

    def amend(self, ref, entry, sl, tp, units, be_trigger=None) -> None:
        o = self.orders.get(ref)
        if o is None:
            return
        o.entry, o.sl, o.tp, o.units, o.be_trigger = entry, sl, tp, units, be_trigger

    def cancel(self, ref: str, reason: str) -> None:
        o = self.orders.pop(ref, None)
        if o is not None and self.listener:
            self.listener.on_order_cancelled(o, reason, self.now)

    def modify_position(self, ref, sl=None, tp=None) -> None:
        p = self.positions.get(ref)
        if p is None:
            return
        if sl is not None:
            p.sl = sl
        if tp is not None:
            p.tp = tp

    def close(self, ref: str, reason: str, units: float | None = None) -> None:
        p = self.positions.get(ref)
        if p is None:
            return
        slip = self.costs.slippage_usd
        price = self.bid - slip if p.side > 0 else self.ask + slip
        if units is not None and units < p.units:
            self._partial(p, price, units)
        else:
            self._close(p, price, "close")

    # ------------------------------------------------------------------ fills and accounting
    def _commission(self, units: float) -> float:
        """Half of the round-turn commission, in account currency."""
        return units * self.symbol.commission_per_unit_rt / 2 * self.symbol.quote_to_account

    def _open(self, o: Order, price: float) -> None:
        self.orders.pop(o.ref, None)
        if o.oco_group:
            for ref, other in list(self.orders.items()):
                if other.oco_group == o.oco_group:
                    self.cancel(ref, "OCO: other side filled")
        price = self.symbol.round_price(price)
        comm = self._commission(o.units)
        self._balance -= comm
        sl, tp = o.sl, o.tp
        p = Position(
            ref=o.ref, module=o.module, side=o.side, units=o.units, initial_units=o.units, entry=price,
            sl=sl, tp=tp, open_ts=self.now, risk_money=o.risk_money, r_dist=o.r_dist, initial_sl=sl,
            planned_entry=o.entry, be_trigger=o.be_trigger, be_offset=o.be_offset,
            partial_price=o.partial_price, partial_fraction=o.partial_fraction,
            realized_pnl=-comm, commission=comm, tag=o.tag, notes=list(o.notes),
        )
        self.positions[o.ref] = p
        o.status = "filled"
        self.fills += 1
        if self.listener:
            self.listener.on_order_filled(o, p, self.now)

    def _partial(self, p: Position, price: float, units: float) -> None:
        units = self.symbol.floor_units(units)
        if units <= 0 or units >= p.units:
            return
        q2a = self.symbol.quote_to_account
        gross = (price - p.entry) * p.side * units * q2a
        comm = self._commission(units)
        self._balance += gross - comm
        p.units = round(p.units - units, 8)
        p.realized_pnl += gross - comm
        p.commission += comm
        p.exit_value += price * units
        p.exited_units += units
        p.partial_done = True
        if self.listener:
            self.listener.on_position_event(p, "partial", {"price": price, "units": units, "pnl": gross - comm}, self.now)

    def _close(self, p: Position, price: float, reason: str) -> None:
        self.positions.pop(p.ref, None)
        price = self.symbol.round_price(price)
        q2a = self.symbol.quote_to_account
        gross = (price - p.entry) * p.side * p.units * q2a
        comm = self._commission(p.units)
        self._balance += gross - comm
        trade = make_closed_trade(p, self.now, price, p.units, reason, gross - comm, comm)
        if self.listener:
            self.listener.on_position_closed(p, trade)

    def _breakeven(self, p: Position) -> None:
        p.sl = self.symbol.round_price(p.entry + p.side * p.be_offset)
        p.be_done = True
        if self.listener:
            self.listener.on_position_event(p, "breakeven", {"sl": p.sl}, self.now)

    # ------------------------------------------------------------------ trigger model
    def _triggers(self, s: float):
        """Yield (bid_level, direction, kind, ref) for everything that can fire.

        ``direction`` UP fires when bid rises to the level, DOWN when it falls to it.
        """
        for ref, o in self.orders.items():
            if o.side > 0:
                yield o.entry - s, UP, "entry", ref
            else:
                yield o.entry, DOWN, "entry", ref
        for ref, p in self.positions.items():
            if p.side > 0:
                yield p.sl, DOWN, "sl", ref
                yield p.tp, UP, "tp", ref
                if p.partial_price is not None and not p.partial_done:
                    yield p.partial_price, UP, "partial", ref
                if p.be_trigger is not None and not p.be_done:
                    yield p.be_trigger, UP, "be", ref
            else:
                yield p.sl - s, UP, "sl", ref
                yield p.tp - s, DOWN, "tp", ref
                if p.partial_price is not None and not p.partial_done:
                    yield p.partial_price - s, DOWN, "partial", ref
                if p.be_trigger is not None and not p.be_done:
                    yield p.be_trigger - s, DOWN, "be", ref

    _ORDER = {"partial": 0, "be": 1, "tp": 2, "sl": 3, "entry": 4}

    def _execute(self, kind: str, ref: str, bid_level: float, s: float, gap: bool) -> None:
        """Fire one trigger. ``bid_level`` is the bid price where it fires."""
        slip = self.costs.slippage_usd
        self.bid, self.ask = bid_level, bid_level + s
        if kind == "entry":
            o = self.orders.get(ref)
            if o is None:
                return
            if o.side > 0:
                price = (bid_level + s if gap else max(o.entry, bid_level + s)) + slip
            else:
                price = (bid_level if gap else min(o.entry, bid_level)) - slip
            self._open(o, price)
            return
        p = self.positions.get(ref)
        if p is None:
            return
        if kind == "sl":
            if p.side > 0:
                price = (bid_level if gap else min(p.sl, bid_level)) - slip
            else:
                price = (bid_level + s if gap else max(p.sl, bid_level + s)) + slip
            at_be = p.be_done and abs(p.sl - (p.entry + p.side * p.be_offset)) < 1e-6
            self._close(p, price, "breakeven" if at_be else "sl")
        elif kind == "tp":
            price = bid_level if p.side > 0 else bid_level + s
            if not gap:
                price = p.tp
            self._close(p, price, "tp")
        elif kind == "partial":
            price = bid_level if p.side > 0 else bid_level + s
            if not gap:
                price = p.partial_price
            units = self.symbol.floor_units(p.initial_units * p.partial_fraction)
            if units < p.units:
                self._partial(p, price, units)
            p.partial_done = True
        elif kind == "be":
            self._breakeven(p)

    def _fire_satisfied(self, price: float, s: float) -> None:
        """Fire every trigger already satisfied at ``price`` (gaps, ticks), at that price."""
        done: set = set()
        for _ in range(64):
            hits = []
            for level, direction, kind, ref in self._triggers(s):
                if (kind, ref) in done:
                    continue
                if (direction == UP and price >= level - 1e-9) or (direction == DOWN and price <= level + 1e-9):
                    hits.append((self._ORDER[kind], level, kind, ref))
            if not hits:
                return
            hits.sort()
            _, level, kind, ref = hits[0]
            done.add((kind, ref))
            self._execute(kind, ref, price, s, gap=True)

    def _walk(self, a: float, b: float, s: float) -> None:
        """Move price from a to b, firing triggers in the order they are reached."""
        if a == b:
            return
        direction = UP if b > a else DOWN
        cur = a
        done: set = set()
        for _ in range(64):
            best = None
            for level, d, kind, ref in self._triggers(s):
                if d != direction or (kind, ref) in done:
                    continue
                if direction == UP and cur - 1e-9 <= level <= b + 1e-9:
                    key = (level, self._ORDER[kind])
                elif direction == DOWN and b - 1e-9 <= level <= cur + 1e-9:
                    key = (-level, self._ORDER[kind])
                else:
                    continue
                if best is None or key < best[0]:
                    best = (key, level, kind, ref)
            if best is None:
                break
            _, level, kind, ref = best
            done.add((kind, ref))
            self._execute(kind, ref, level, s, gap=False)
            cur = level
        self.bid, self.ask = b, b + s

    def process_bar(self, bar: Bar) -> None:
        s = bar.s if bar.s > 0 else self.costs.spread_usd
        self.now = bar.t
        if self.orders or self.positions:
            self.bid, self.ask = bar.o, bar.o + s
            self._fire_satisfied(bar.o, s)
            path = (bar.o, bar.l, bar.h, bar.c) if bar.c >= bar.o else (bar.o, bar.h, bar.l, bar.c)
            for a, b in zip(path, path[1:]):
                if self.orders or self.positions:
                    self._walk(a, b, s)
            for p in self.positions.values():
                if p.side > 0:
                    p.best = max(p.best, bar.h)
                    p.worst = min(p.worst, bar.l)
                else:
                    p.best = min(p.best, bar.l + s)
                    p.worst = max(p.worst, bar.h + s)
        self.bid, self.ask = bar.c, bar.c + s
        self.now = bar.t + 60

    def process_quote(self, ts: int, bid: float, ask: float) -> None:
        """Tick-level processing for paper trading on live prices."""
        self.now = ts
        s = max(ask - bid, 0.0)
        self.bid, self.ask = bid, ask
        if self.orders or self.positions:
            self._fire_satisfied(bid, s)
            for p in self.positions.values():
                mark = p.mark(bid, ask)
                if (mark - p.best) * p.side > 0:
                    p.best = mark
                if (mark - p.worst) * p.side < 0:
                    p.worst = mark
        self.bid, self.ask = bid, ask
