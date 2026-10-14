"""Module D: PM-fix reversion (experimental, 0–2 trades). Off until it passes validation.

1. Window 09:45–10:30 New York.
2. Condition: price has moved at least 1.5 x M15 ATR(14) in one direction since 08:30
   New York, and the M5 bar's extreme came within $2 of a $20 level.
3. Trigger: that M5 candle closes back away from the level (a rejection close: a bearish
   candle closing below the level after an up move, mirrored for down moves).
4. Entry on that candle's close, against the prior move.
5. Stop: $2.5 beyond the level or just beyond the move's extreme, whichever is further.
6. Target: 1.5R or the anchored VWAP, whichever is nearer; skipped under 1.2R.
At most one trade per $20 level and two per day.
"""

from __future__ import annotations

from ..bars import Bar
from ..levels import nearest_level
from ..models import LONG, SHORT, Order, Position, side_name
from ..risk import Signal
from .base import Module


class ModuleD(Module):
    name = "D"
    title = "PM-fix reversion (experimental)"
    window = "09:45–10:30 New York"

    def reset_day(self) -> None:
        super().reset_day()
        self.anchor: float | None = None
        self.hi: float | None = None
        self.lo: float | None = None
        self.used_levels: set[float] = set()
        self.status = "waiting for 08:30 New York"

    def on_m1(self, bar: Bar) -> None:
        w = self.e.w
        if bar.t < w.move_anchor or bar.t >= w.pmfix_end:
            return
        if self.anchor is None:
            self.anchor = bar.o
            self.hi, self.lo = bar.h, bar.l
            self.status = f"measuring the move from {bar.o:.2f}"
        else:
            self.hi = max(self.hi, bar.h)
            self.lo = min(self.lo, bar.l)

    def on_m5(self, bar: Bar) -> None:
        e, cfg, w = self.e, self.cfg, self.e.w
        if not (w.pmfix_start < bar.close_time <= w.pmfix_end):
            return
        if self.trades >= cfg.max_trades or self.anchor is None:
            return
        atr15 = e.atr_m15
        if not atr15:
            return
        move = bar.c - self.anchor
        need = cfg.move_atr_mult * atr15
        self.detail = {"anchor": self.anchor, "move": move, "need": need, "atr_m15": atr15}
        if abs(move) < need:
            self.status = f"move {move:+.2f} is under {cfg.move_atr_mult} x M15 ATR ({need:.2f})"
            return
        step = e.s.levels.step
        if move > 0:
            lvl = nearest_level(bar.h, step)
            near = abs(bar.h - lvl) <= cfg.level_proximity
            rejected = bar.c < bar.o and bar.c < lvl
            side = SHORT
        else:
            lvl = nearest_level(bar.l, step)
            near = abs(bar.l - lvl) <= cfg.level_proximity
            rejected = bar.c > bar.o and bar.c > lvl
            side = LONG
        if not near:
            self.status = f"stretched {move:+.2f}, not near a $20 level"
            return
        if not rejected:
            self.status = f"stretched into {lvl:.0f}, waiting for a rejection close"
            return
        if lvl in self.used_levels:
            return
        self.used_levels.add(lvl)
        if side == SHORT:
            entry = e.bid
            stop = max(lvl + cfg.stop_beyond_level, self.hi + cfg.extreme_buffer)
        else:
            entry = e.ask
            stop = min(lvl - cfg.stop_beyond_level, self.lo - cfg.extreme_buffer)
        sig = Signal(
            module=self.name, side=side, kind="market", entry=entry, stop=stop, target_r=cfg.target_r,
            target_price=e.vwap.value, target_mode="nearer", apply_level_rules=cfg.apply_level_rules,
            tag=f"pm-fix-{lvl:.0f}",
        )
        if e.submit(sig) is None:
            self.status = f"rejection at {lvl:.0f} skipped (see skips)"

    def on_fill(self, order: Order, position: Position) -> None:
        super().on_fill(order, position)
        self.status = f"{side_name(position.side)} filled @ {position.entry:.2f}"
