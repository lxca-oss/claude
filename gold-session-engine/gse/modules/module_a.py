"""Module A: London breakout of the Asian range (0–1 trade).

1. Asian range = high/low of 00:00–07:00 London.
2. Skip the day if the range is under 0.30% or over 1.60% of price.
3. At 07:00 London place a buy stop above the high and a sell stop below the low, each
   with a small buffer; when one fills the other is cancelled (OCO).
4. Stop: opposite side of the range, capped at 0.6% of price.
5. Target 2.5R; stop to break-even at 1.5R.
6. One Module A trade per day.
"""

from __future__ import annotations

from ..bars import Bar
from ..models import LONG, SHORT, Order, Position, ClosedTrade, side_name
from ..risk import Signal
from .base import Module


class ModuleA(Module):
    name = "A"
    title = "London range breakout"
    window = "07:00–10:00 London"

    def reset_day(self) -> None:
        super().reset_day()
        self.range_checked = False
        self.range_ok = False
        self.placed = False
        self.filled = False
        self.news_cancelled = False
        self.status = "building the Asian range (00:00–07:00 London)"

    def expiry(self) -> int:
        w = self.e.w
        return w.london_end if self.cfg.order_expiry == "window_end" else w.no_new_trades

    def on_m1(self, bar: Bar) -> None:
        e, cfg, w = self.e, self.cfg, self.e.w
        now = e.now
        if now < w.london_start:
            return
        if not self.range_checked:
            self._check_range()
        if now >= self.expiry():
            if self.placed and not self.filled and not self.refs and self.range_ok:
                self.status = "no breakout before the orders expired"
            return
        if not self.range_ok or self.filled or self.trades >= cfg.max_trades or self.refs:
            return
        if self.placed and not self.news_cancelled:
            return
        ok, why = e.can_open(self.name)
        if not ok:
            self.status = f"waiting: {why}"
            return
        self._place(replacing=self.placed)

    def _check_range(self) -> None:
        e, cfg = self.e, self.cfg
        self.range_checked = True
        hi, lo = e.asian_high, e.asian_low
        if hi is None or e.asian_coverage < e.s.sessions.min_asian_coverage:
            self.status = f"skip: Asian range has only {e.asian_coverage:.0%} of its data"
            e.skip(self.name, "not enough Asian-range data")
            return
        mid = (hi + lo) / 2
        width_pct = (hi - lo) / mid * 100
        self.detail = {"high": hi, "low": lo, "width": hi - lo, "width_pct": width_pct}
        if width_pct < cfg.range_min_pct:
            self.status = f"skip: range {width_pct:.2f}% < {cfg.range_min_pct}%"
            e.skip(self.name, "range too narrow")
        elif width_pct > cfg.range_max_pct:
            self.status = f"skip: range {width_pct:.2f}% > {cfg.range_max_pct}%"
            e.skip(self.name, "range too wide")
        else:
            self.range_ok = True
            self.status = f"range {width_pct:.2f}% OK"

    def _place(self, replacing: bool) -> None:
        e, cfg = self.e, self.cfg
        hi, lo = e.asian_high, e.asian_low
        group = f"A{e.day:%y%m%d}"
        sides = []
        for side in (LONG, SHORT):
            if side == LONG:
                entry = hi + cfg.entry_buffer
                stop = lo
                cap = cfg.stop_cap_pct / 100 * entry
                if entry - stop > cap:
                    stop = entry - cap
                crossed = e.ask >= entry
            else:
                entry = lo - cfg.entry_buffer
                stop = hi
                cap = cfg.stop_cap_pct / 100 * entry
                if stop - entry > cap:
                    stop = entry + cap
                crossed = e.bid <= entry
            sides.append((side, entry, stop, crossed))

        crossed_sides = [s for s in sides if s[3]]
        at_open = e.now - e.w.london_start <= 120
        if crossed_sides:
            if replacing or not at_open:
                # the breakout happened while we could not have orders out (news pause, bot started
                # late): do not chase it
                why = "during a news pause" if replacing else "before the bot placed its orders"
                self.status = f"price already broke out {why}: not chasing"
                e.skip(self.name, "breakout happened before orders were working")
                self.placed, self.news_cancelled = True, False
                return
            side, entry, stop, _ = crossed_sides[0]
            plan_sides = [(side, e.ask if side == LONG else e.bid, stop, "market")]
        else:
            plan_sides = [(side, entry, stop, "stop") for side, entry, stop, _ in sides]

        placed = []
        for side, entry, stop, kind in plan_sides:
            sig = Signal(
                module=self.name, side=side, kind=kind, entry=entry, stop=stop, target_r=cfg.target_r,
                be_at_r=cfg.breakeven_r or None, apply_level_rules=cfg.apply_level_rules,
                oco_group=group, expiry_ts=self.expiry() if kind == "stop" else None, tag="breakout",
            )
            ref = e.submit(sig)
            if ref and kind == "stop":
                self.refs.add(ref)
                placed.append(f"{side_name(side)} stop {entry:.2f}")
            if self.filled:
                break
        self.placed = True
        self.news_cancelled = False
        if placed:
            self.status = "working: " + ", ".join(placed)
        elif not self.filled:
            self.status = "no valid side after the shared rules (see skips)"

    def on_fill(self, order: Order, position: Position) -> None:
        super().on_fill(order, position)
        self.filled = True
        self.status = f"{side_name(position.side)} filled @ {position.entry:.2f}"

    def on_cancel(self, order: Order, reason: str) -> None:
        super().on_cancel(order, reason)
        if reason.startswith("news") and not self.filled:
            self.news_cancelled = True
            self.status = f"orders pulled for news, will re-place if price is still inside the range"

    def restore_trade(self, t: dict) -> None:
        super().restore_trade(t)
        self.filled = True
        self.placed = True

    def on_close(self, trade: ClosedTrade) -> None:
        super().on_close(trade)
        self.status = f"done for the day: {trade.exit_reason} {trade.r:+.2f}R"
