"""Strategy engine: shared rules plus the four modules.

The engine is fed closed M1 bid bars (with spread). It keeps the M5/M15 indicators,
the Asian range and the anchored VWAP, enforces the shared rules (daily limits, news
blackout, session close, weekend, one direction at a time) and routes order and
position events between the broker and the modules.

The same engine runs in backtests (with the simulator), replays, paper trading and live
trading on cTrader, so what is tested is exactly what trades.
"""

from __future__ import annotations

import logging
import time as _time
from collections import Counter, deque
from datetime import date
from typing import Callable

from .bars import Aggregator, Bar
from .broker.base import Broker
from .config import Settings
from .indicators import ATR, EMA, AnchoredVWAP
from .levels import level_above, level_below
from .models import ClosedTrade, Order, Position, side_name
from .news import NewsCalendar
from .risk import DailyRisk, Signal, Skip, TradePlanner, classify
from .sessions import DayWindows, SessionClock, trading_date, ts_to_dt, LONDON, NEW_YORK

log = logging.getLogger(__name__)

EmitFn = Callable[[str, str, dict], None]


class StrategyEngine:
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        news: NewsCalendar | None = None,
        emit: EmitFn | None = None,
        record_events: bool = True,
    ):
        from .modules import build_modules

        self.s = settings
        self.broker = broker
        broker.set_listener(self)
        self.symbol = broker.symbol
        self.clock = SessionClock(settings.sessions)
        self.news = news or NewsCalendar(settings.news)
        self.planner = TradePlanner(settings.risk, settings.levels, broker.symbol)
        self.emit_cb = emit
        self.record_events = record_events
        self.initial_balance = broker.balance()

        # market state
        self.now = 0
        self.bid: float | None = None
        self.ask: float | None = None
        self.last_bar: Bar | None = None
        self._quote_ts = 0
        self.m1: deque[Bar] = deque(maxlen=240)
        self.m5: deque[dict] = deque(maxlen=600)  # M5 bars with indicator values, for charts
        self.agg5 = Aggregator(300)
        self.agg15 = Aggregator(900)
        self.atr5 = ATR(14)
        self.atr15 = ATR(14)
        self.ema5 = EMA(settings.module_c.ema_period)
        self.ema15 = EMA(settings.module_c.ema_period)
        self.vwap = AnchoredVWAP()

        # day state
        self.day: date | None = None
        self.w: DayWindows | None = None
        self.daily: DailyRisk | None = None
        self.asian_high: float | None = None
        self.asian_low: float | None = None
        self.asian_bars = 0
        self.history: list[dict] = []  # one summary row per finished day

        # orders and positions (by ref)
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.skips: Counter = Counter()
        self._skip_seen: set = set()
        self.events: deque[dict] = deque(maxlen=400)
        self._seq = 0
        self._close_reasons: dict[str, str] = {}

        self.trading = True  # False during warm-up
        self.paused = False  # manual pause from the terminal
        self.modules = build_modules(self)

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, message: str, **data) -> None:
        if not self.record_events and kind not in ("fill", "close"):
            return
        ev = {"ts": self.now or int(_time.time()), "kind": kind, "message": message, "data": data}
        self.events.append(ev)
        if self.emit_cb:
            try:
                self.emit_cb(kind, message, ev)
            except Exception:  # never let the UI/journal break trading
                log.exception("emit callback failed")

    def skip(self, module: str, reason: str) -> None:
        """Record why a signal did not become a trade (counted once per day/module/reason)."""
        bucket = _reason_bucket(reason)
        key = (self.day, module, bucket)
        if key in self._skip_seen:
            return
        self._skip_seen.add(key)
        if self.trading:
            self.skips[f"{module}: {bucket}"] += 1
            self.emit("skip", f"Module {module} skipped: {reason}", module=module, reason=reason)

    # ------------------------------------------------------------------ properties
    @property
    def atr_m5(self) -> float | None:
        return self.atr5.value

    @property
    def atr_m15(self) -> float | None:
        return self.atr15.value

    @property
    def asian_coverage(self) -> float:
        if not self.w:
            return 0.0
        minutes = (self.w.asian_end - self.w.asian_start) / 60
        return self.asian_bars / minutes if minutes else 0.0

    @property
    def asian_range_complete(self) -> bool:
        return self.w is not None and self.now >= self.w.asian_end

    # ------------------------------------------------------------------ bar processing
    def on_quote(self, ts: int, bid: float, ask: float) -> None:
        """Live price between bars (keeps the snapshot and market-order estimates current)."""
        self.bid, self.ask = bid, ask
        self._quote_ts = ts

    def on_bar(self, bar: Bar) -> None:
        now = bar.t + 60
        day = trading_date(bar.t)
        if day != self.day:
            self._new_day(day)
        self.now = now
        self.last_bar = bar
        if self._quote_ts < now:
            self.bid, self.ask = bar.c, bar.c + bar.s
        self.m1.append(bar)
        w = self.w

        if w.asian_start <= bar.t < w.asian_end:
            self.asian_bars += 1
            self.asian_high = bar.h if self.asian_high is None else max(self.asian_high, bar.h)
            self.asian_low = bar.l if self.asian_low is None else min(self.asian_low, bar.l)

        if bar.t >= w.vwap_anchor:
            if self.vwap.anchor != w.vwap_anchor:
                self.vwap.reset(w.vwap_anchor)
            self.vwap.update(bar.h, bar.l, bar.c, bar.v)

        m5_done = self.agg5.update(bar)
        for b in m5_done:
            self.atr5.update(b.h, b.l, b.c)
            self.ema5.update(b.c)
            self.m5.append({
                **b.to_dict(), "ema": self.ema5.value,
                "vwap": self.vwap.value if b.t >= w.vwap_anchor else None,
            })
        for b in self.agg15.update(bar):
            self.atr15.update(b.h, b.l, b.c)
            self.ema15.update(b.c)

        if w.is_weekend:
            return

        self._shared_rules(now)

        for m in self.modules:
            if not m.enabled:
                continue
            for b in m5_done:
                m.on_m5(b)
            m.on_m1(bar)

    def on_clock(self, ts: int) -> None:
        """Run the time-based shared rules from the wall clock (live), in case no bar arrives."""
        if self.w is None or not self.trading or ts <= self.now or trading_date(ts) != self.day:
            return
        if not self.w.is_weekend:
            self._shared_rules(ts)

    def _new_day(self, day: date) -> None:
        if self.daily is not None and self.day is not None:
            self.history.append({**self.daily.to_dict(), "end_balance": self.broker.balance()})
            if len(self.history) > 5000:
                del self.history[:1000]
        self.day = day
        self.w = self.clock.windows(day)
        self.daily = DailyRisk(day=day, start_balance=self.broker.balance())
        self.asian_high = self.asian_low = None
        self.asian_bars = 0
        self._skip_seen = set()
        for m in self.modules:
            m.reset_day()

    # ------------------------------------------------------------------ shared rules
    def _shared_rules(self, now: int) -> None:
        w, d = self.w, self.daily

        for ref, o in list(self.orders.items()):
            if o.expiry_ts and now >= o.expiry_ts:
                self.cancel(ref, "expired")

        # daily equity stops
        if d.stopped is None and self.trading and d.start_balance > 0:
            eq = self.broker.equity()
            d.min_equity = eq if d.min_equity is None else min(d.min_equity, eq)
            d.max_equity = eq if d.max_equity is None else max(d.max_equity, eq)
            pct = (eq - d.start_balance) / d.start_balance * 100
            if pct <= -self.s.risk.daily_loss_stop_pct:
                self._stop_day(f"daily loss stop {pct:.2f}%", flatten=self.s.risk.flatten_on_daily_stop)
            elif pct >= self.s.risk.daily_profit_stop_pct:
                self._stop_day(f"daily profit stop +{pct:.2f}%", flatten=self.s.risk.flatten_on_daily_stop)

        # session close
        if now >= w.no_new_trades and self.orders:
            for ref in list(self.orders):
                self.cancel(ref, "no new trades after 11:00 NY")
        if now >= w.flat:
            for ref, p in list(self.positions.items()):
                if p.module == "A" and self.s.module_a.exit_mode == "server_2100":
                    if now >= w.server_close:
                        self.close(ref, "server close 21:00")
                    continue
                self.close(ref, "session close 11:30 NY")

        # news blackout: pull pending entries; modules re-place afterwards if still valid
        ev = self.news.blackout(now) if self.orders else None
        if ev is not None:
            for ref in list(self.orders):
                self.cancel(ref, f"news: {ev.title}")

    def _stop_day(self, reason: str, flatten: bool) -> None:
        d = self.daily
        if d.stopped:
            return
        d.stopped = reason
        self.emit("risk", f"Trading stopped for the day: {reason}", reason=reason)
        for ref in list(self.orders):
            self.cancel(ref, f"day stopped: {reason}")
        if flatten:
            for ref in list(self.positions):
                self.close(ref, f"day stopped: {reason}")

    def can_open(self, module: str) -> tuple[bool, str]:
        d, w, now = self.daily, self.w, self.now
        if not self.trading:
            return False, "warm-up"
        if self.paused:
            return False, "paused from the terminal"
        if d.stopped:
            return False, d.stopped
        if d.trades >= self.s.risk.max_trades_per_day:
            return False, f"max {self.s.risk.max_trades_per_day} trades/day reached"
        if w.is_weekend:
            return False, "weekend"
        if now >= w.no_new_trades:
            return False, "after 11:00 NY"
        if w.weekday == 0 and now < w.london_start:
            return False, "Monday before London open"
        ev = self.news.blackout(now)
        if ev is not None:
            return False, f"news blackout: {ev.title}"
        return True, ""

    # ------------------------------------------------------------------ order helpers used by modules
    def _new_ref(self, module: str) -> str:
        self._seq += 1
        stamp = ts_to_dt(self.now).strftime("%y%m%d%H%M")
        return f"{module}{stamp}{self._seq % 1000:03d}"

    def sizing_balance(self) -> float:
        if self.s.risk.risk_basis == "initial":
            return self.initial_balance
        return self.broker.balance()

    def submit(self, sig: Signal) -> str | None:
        ok, why = self.can_open(sig.module)
        if not ok:
            self.skip(sig.module, why)
            return None
        plan = self.planner.plan(sig, self.atr_m5, self.sizing_balance())
        if isinstance(plan, Skip):
            self.skip(sig.module, str(plan))
            return None
        if plan.kind == "market":
            self._close_opposite(plan.side, f"opposite signal from module {sig.module}")
        ref = self._new_ref(sig.module)
        order = Order.from_plan(ref, plan, self.now)
        if plan.kind == "stop":
            self.orders[ref] = order
        self.emit(
            "order",
            f"{sig.module} {side_name(plan.side)} {plan.kind} {plan.units:g} oz @ {plan.entry:.2f} "
            f"SL {plan.sl:.2f} TP {plan.tp:.2f} ({plan.target_r:.2f}R, risk {plan.risk_money:.2f})",
            order=order.to_dict(),
        )
        self.broker.place(order)
        return ref

    def amend_entry(self, ref: str, sig: Signal) -> str | None:
        order = self.orders.get(ref)
        if order is None:
            return None
        plan = self.planner.plan(sig, self.atr_m5, self.sizing_balance())
        if isinstance(plan, Skip):
            self.skip(sig.module, str(plan))
            self.cancel(ref, f"setup no longer valid: {plan}")
            return None
        if (abs(plan.entry - order.entry) < 0.05 and abs(plan.sl - order.sl) < 0.05
                and abs(plan.tp - order.tp) < 0.05 and plan.units == order.units):
            return ref
        order.entry, order.sl, order.tp, order.units = plan.entry, plan.sl, plan.tp, plan.units
        order.risk_money, order.r_dist, order.be_trigger = plan.risk_money, plan.r_dist, plan.be_trigger
        order.notes = plan.notes
        self.broker.amend(ref, plan.entry, plan.sl, plan.tp, plan.units, plan.be_trigger)
        return ref

    def cancel(self, ref: str, reason: str) -> None:
        if ref in self.orders:
            self.broker.cancel(ref, reason)

    def close(self, ref: str, reason: str, units: float | None = None) -> None:
        if ref in self.positions:
            if units is None:
                self._close_reasons[ref] = reason
            self.broker.close(ref, reason, units)

    def _close_opposite(self, side: int, reason: str) -> None:
        for ref, p in list(self.positions.items()):
            if p.side != side:
                self.close(ref, reason)

    def flatten_all(self, reason: str = "manual flatten") -> None:
        for ref in list(self.orders):
            self.cancel(ref, reason)
        for ref in list(self.positions):
            self.close(ref, reason)

    # ------------------------------------------------------------------ restart recovery (live)
    def restore_today(self, trades: list[dict]) -> None:
        """Rebuild today's counters from journal rows after a restart (call after warm-up)."""
        d = self.daily
        if d is None:
            return
        for t in sorted(trades, key=lambda r: r.get("open_ts") or 0):
            if t.get("day") != d.day.isoformat():
                continue
            d.trades += 1
            d.module_trades[t["module"]] = d.module_trades.get(t["module"], 0) + 1
            if t.get("status") == "closed" and t.get("r") is not None:
                d.realized_pnl += t.get("pnl") or 0.0
                result = classify(t["r"], self.s.risk.scratch_r)
                if result == "loss":
                    d.losses += 1
                    d.consecutive_losses += 1
                    d.module_losses[t["module"]] = d.module_losses.get(t["module"], 0) + 1
                elif result == "win":
                    d.wins += 1
                    d.consecutive_losses = 0
            m = self._module(t["module"])
            if m:
                m.restore_trade(t)
        if d.consecutive_losses >= self.s.risk.max_consecutive_losses:
            d.stopped = f"{d.consecutive_losses} consecutive losses"
        if d.trades:
            self.emit("info", f"Restored today's state from the journal: {d.trades} trades, "
                              f"{d.consecutive_losses} consecutive losses")

    def adopt_position(self, p: Position) -> None:
        self.positions[p.ref] = p
        self.emit("info", f"Adopted open position {p.ref} ({p.module} {side_name(p.side)} {p.units:g} oz)")

    # ------------------------------------------------------------------ broker callbacks
    def _module(self, name: str):
        for m in self.modules:
            if m.name == name:
                return m
        return None

    def on_order_filled(self, order: Order, position: Position, ts: int) -> None:
        self.orders.pop(order.ref, None)
        order.status = "filled"
        self.positions[position.ref] = position
        d = self.daily
        if d is not None:
            d.trades += 1
            d.module_trades[order.module] = d.module_trades.get(order.module, 0) + 1
        self.emit(
            "fill",
            f"{order.module} {side_name(position.side)} {position.units:g} oz filled @ {position.entry:.2f} "
            f"(planned {order.entry:.2f}) SL {position.sl:.2f} TP {position.tp:.2f}",
            position=position.to_dict(),
        )
        self._close_opposite(position.side, f"opposite fill from module {order.module}")
        if d is not None and d.trades >= self.s.risk.max_trades_per_day:
            for ref in list(self.orders):
                self.cancel(ref, "max trades/day reached")
        m = self._module(order.module)
        if m:
            m.on_fill(order, position)

    def on_order_cancelled(self, order: Order, reason: str, ts: int) -> None:
        self.orders.pop(order.ref, None)
        order.status = "cancelled"
        self.emit("cancel", f"{order.module} order {order.ref} cancelled: {reason}", ref=order.ref, reason=reason)
        m = self._module(order.module)
        if m:
            m.on_cancel(order, reason)

    def on_order_rejected(self, order: Order, reason: str, ts: int) -> None:
        self.orders.pop(order.ref, None)
        order.status = "rejected"
        self.emit("reject", f"{order.module} order {order.ref} rejected: {reason}", ref=order.ref, reason=reason)
        m = self._module(order.module)
        if m:
            m.on_cancel(order, f"rejected: {reason}")

    def on_position_event(self, position: Position, kind: str, info: dict, ts: int) -> None:
        if kind == "partial":
            self.emit("partial", f"{position.module} partial close {info.get('units', 0):g} oz @ "
                      f"{info.get('price', 0):.2f}, stop to break-even", ref=position.ref, **info)
        elif kind == "breakeven":
            self.emit("breakeven", f"{position.module} stop moved to break-even {position.sl:.2f}",
                      ref=position.ref, **info)
        else:
            self.emit("position", f"{position.module} {kind}", ref=position.ref, **info)

    def on_position_closed(self, position: Position, trade: ClosedTrade) -> None:
        self.positions.pop(position.ref, None)
        reason = self._close_reasons.pop(position.ref, None)
        if reason and trade.exit_reason in ("close", "manual"):
            trade.exit_reason = reason
        self.closed.append(trade)
        d = self.daily
        result = classify(trade.r, self.s.risk.scratch_r)
        if d is not None and trading_date(trade.open_ts) == d.day:
            d.realized_pnl += trade.pnl
            if result == "loss":
                d.losses += 1
                d.consecutive_losses += 1
                d.module_losses[trade.module] = d.module_losses.get(trade.module, 0) + 1
            elif result == "win":
                d.wins += 1
                d.consecutive_losses = 0
        self.emit(
            "close",
            f"{trade.module} {side_name(trade.side)} closed @ {trade.exit:.2f} ({trade.exit_reason}): "
            f"{trade.r:+.2f}R, {trade.pnl:+.2f}",
            trade=trade.to_dict(),
        )
        if d is not None and d.consecutive_losses >= self.s.risk.max_consecutive_losses and not d.stopped:
            self._stop_day(f"{d.consecutive_losses} consecutive losses", flatten=False)
        m = self._module(trade.module)
        if m:
            m.on_close(trade)

    # ------------------------------------------------------------------ state for the terminal
    def snapshot(self) -> dict:
        w, d = self.w, self.daily
        bid, ask = self.bid, self.ask
        price = bid if bid is not None else None
        levels = {}
        if price:
            levels = {
                "above": level_above(price, self.s.levels.step),
                "below": level_below(price, self.s.levels.step),
            }
        asian = None
        if self.asian_high is not None and self.asian_low is not None:
            mid = (self.asian_high + self.asian_low) / 2
            asian = {
                "high": self.asian_high, "low": self.asian_low, "mid": mid,
                "width": self.asian_high - self.asian_low,
                "width_pct": (self.asian_high - self.asian_low) / mid * 100 if mid else 0,
                "coverage": self.asian_coverage, "complete": self.asian_range_complete,
            }
        ev = self.news.blackout(self.now) if self.now else None
        nxt = self.news.next_event(self.now) if self.now else None
        equity = self.broker.equity()
        return {
            "now": self.now,
            "now_london": ts_to_dt(self.now, LONDON).strftime("%a %d %b %H:%M") if self.now else None,
            "now_new_york": ts_to_dt(self.now, NEW_YORK).strftime("%a %d %b %H:%M") if self.now else None,
            "day": self.day.isoformat() if self.day else None,
            "bid": bid, "ask": ask,
            "spread": (ask - bid) if bid is not None and ask is not None else None,
            "trading": self.trading, "paused": self.paused,
            "balance": self.broker.balance(), "equity": equity,
            "daily": d.to_dict() if d else None,
            "daily_pnl_pct": ((equity - d.start_balance) / d.start_balance * 100) if d and d.start_balance else 0.0,
            "limits": {
                "risk_per_trade_pct": self.s.risk.risk_per_trade_pct,
                "max_trades": self.s.risk.max_trades_per_day,
                "daily_loss_stop_pct": self.s.risk.daily_loss_stop_pct,
                "daily_profit_stop_pct": self.s.risk.daily_profit_stop_pct,
                "max_consecutive_losses": self.s.risk.max_consecutive_losses,
            },
            "windows": w.rows() if w else [],
            "asian": asian,
            "indicators": {
                "atr_m5": self.atr5.value, "atr_m15": self.atr15.value,
                "ema_m5": self.ema5.value, "ema_m15": self.ema15.value,
                "ema_m15_slope": self.ema15.slope(self.s.module_c.slope_bars),
                "vwap": self.vwap.value if w and self.now >= w.vwap_anchor else None,
                "min_stop": self.planner.min_stop(self.atr5.value),
            },
            "levels": levels,
            "news": {
                "blackout": ev.to_dict() if ev else None,
                "next": nxt.to_dict() if nxt else None,
            },
            "modules": [m.snapshot() for m in self.modules],
            "positions": [p.to_dict(bid, ask) if bid is not None else p.to_dict() for p in self.positions.values()],
            "orders": [o.to_dict() for o in self.orders.values()],
            "skips": dict(self.skips),
        }

    def chart(self, limit: int = 300) -> dict:
        bars = list(self.m5)[-limit:]
        cur = self.agg5.cur
        if cur is not None:
            bars = bars + [{**cur.to_dict(), "ema": None, "vwap": self.vwap.value, "forming": True}]
        return {
            "bars": bars,
            "asian": {"high": self.asian_high, "low": self.asian_low} if self.asian_high is not None else None,
            "positions": [p.to_dict() for p in self.positions.values()],
            "orders": [o.to_dict() for o in self.orders.values()],
            "level_step": self.s.levels.step,
        }


def _reason_bucket(reason: str) -> str:
    """Group similar skip reasons (numbers removed) so the counts are readable."""
    import re

    reason = re.sub(r"news blackout: .*", "news blackout", reason)
    reason = re.sub(r"room [0-9.\-]+R < [0-9.]+R", "less than the minimum room to target", reason)
    reason = re.sub(r"position size .*", "position size below broker minimum", reason)
    reason = re.sub(r"daily (loss|profit) stop .*", r"daily \1 stop", reason)
    reason = re.sub(r"[0-9]+ consecutive losses", "consecutive-loss stop", reason)
    return reason
