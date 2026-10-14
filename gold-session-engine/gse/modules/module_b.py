"""Module B: Asian sweep and reclaim (0–2 trades). A hypothesis until it beats random entries.

1. Window 07:00–10:00 London.
2. Sweep: price trades at least $1 beyond the Asian high or low.
3. Reclaim: an M5 candle closes back inside the range within 3 bars of the sweep
   (the sweep bar itself counts as bar 0).
4. Entry at market on the reclaim close, back into the range.
5. Stop beyond the sweep wick plus max($1.50, 0.5 x M5 ATR(14)).
6. 50% off at the range midpoint, the rest $1 short of the opposite edge; stop to
   break-even after the first target.
7. An opposite Module A position is closed first (engine one-direction rule).
8. At most one trade per side (high sweep, low sweep) per day.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..bars import Bar
from ..models import LONG, SHORT, Order, Position, side_name
from ..risk import Signal
from .base import Module


@dataclass
class _Sweep:
    active: bool = False
    extreme: float = 0.0
    bars: int = 0
    need_reset: bool = False
    trades: int = 0


class ModuleB(Module):
    name = "B"
    title = "Asian sweep and reclaim"
    window = "07:00–10:00 London"

    def reset_day(self) -> None:
        super().reset_day()
        # keyed by the direction of the resulting trade: a sweep of the high gives a short
        self.sides = {SHORT: _Sweep(), LONG: _Sweep()}
        self.status = "waiting for the London window"

    def on_m5(self, bar: Bar) -> None:
        e, cfg, w = self.e, self.cfg, self.e.w
        if not (w.london_start < bar.close_time <= w.london_end):
            return
        hi, lo = e.asian_high, e.asian_low
        if hi is None or e.asian_coverage < e.s.sessions.min_asian_coverage:
            self.status = "no valid Asian range"
            return
        self.detail = {"high": hi, "low": lo, "mid": (hi + lo) / 2}
        inside = lo < bar.c < hi
        for side, st in self.sides.items():
            if st.trades >= cfg.max_per_side:
                continue
            if side == SHORT:
                beyond = bar.h >= hi + cfg.sweep_min
                wick = bar.h
            else:
                beyond = bar.l <= lo - cfg.sweep_min
                wick = bar.l
            if st.need_reset:
                if inside:
                    st.need_reset = False
                continue
            if not st.active:
                if not beyond:
                    continue
                st.active, st.bars, st.extreme = True, 0, wick
                self.status = f"sweep of the {'high' if side == SHORT else 'low'} ({wick:.2f}), watching for a reclaim"
            else:
                st.bars += 1
                st.extreme = max(st.extreme, wick) if side == SHORT else min(st.extreme, wick)
            if inside:
                st.active = False
                self._enter(side, st)
            elif st.bars >= cfg.reclaim_bars:
                st.active = False
                st.need_reset = True
                self.status = f"sweep of the {'high' if side == SHORT else 'low'} did not reclaim within {cfg.reclaim_bars} bars"

    def _enter(self, side: int, st: _Sweep) -> None:
        e, cfg = self.e, self.cfg
        hi, lo = e.asian_high, e.asian_low
        buf = max(cfg.stop_buffer_min, cfg.stop_buffer_atr * (e.atr_m5 or 0.0))
        mid = (hi + lo) / 2
        if side == SHORT:
            entry, stop, final = e.bid, st.extreme + buf, lo + cfg.final_target_offset
        else:
            entry, stop, final = e.ask, st.extreme - buf, hi - cfg.final_target_offset
        sig = Signal(
            module=self.name, side=side, kind="market", entry=entry, stop=stop, target_mode="price",
            target_price=final, partial_price=mid, partial_fraction=cfg.partial_fraction, be_after_partial=True,
            apply_level_rules=cfg.apply_level_rules, tag="sweep-reclaim",
            notes=[f"sweep extreme {st.extreme:.2f}"],
        )
        ref = e.submit(sig)
        if ref is None:
            self.status = (f"reclaim {side_name(side)} seen during warm-up (not traded)" if not e.trading
                           else f"reclaim {side_name(side)} signal skipped (see skips)")

    def restore_trade(self, t: dict) -> None:
        super().restore_trade(t)
        side = LONG if (t.get("side") or 0) > 0 else SHORT
        self.sides[side].trades += 1

    def on_fill(self, order: Order, position: Position) -> None:
        super().on_fill(order, position)
        self.sides[position.side].trades += 1
        self.status = f"{side_name(position.side)} filled @ {position.entry:.2f}"
