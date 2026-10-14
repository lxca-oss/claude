"""Module C: New York continuation pullback (0–3 trades).

1. Window 08:20–11:00 New York, subject to the news filter.
2. Bias long when the M5 close is above the VWAP anchored at 07:00 London AND the M15
   20 EMA slopes up (EMA now > EMA ``slope_bars`` M15 bars ago); short when both point
   down; no trade when they disagree.
3. Setup: an M5 bar that touches the M5 20 EMA or the anchored VWAP (within
   ``touch_tolerance``). The setup stays armed for ``setup_expiry_bars`` M5 bars after
   the last touch, and is dropped when the bias changes.
4. Trigger: an M1 break of the pullback's last swing high (long) / low (short). The
   swing is the most recent confirmed M1 fractal (``swing_strength`` bars each side)
   since the pullback started; a stop order sits just beyond it and follows lower highs
   down. If an M1 bar closes through the swing before an order could be placed, the
   entry is taken at market on that bar's close.
5. Stop beyond the pullback extreme, respecting the minimum stop distance.
6. Target 2R (1.5–2R), adjusted by the $20-level rule; skipped with less than 1.2R room.
7. Module C stops for the day after 2 losses; at most 3 trades.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..bars import Bar
from ..models import LONG, SHORT, ClosedTrade, Order, Position, side_name
from ..risk import Signal
from .base import Module


@dataclass
class _Setup:
    side: int
    extreme: float
    expires: int
    start_ts: int
    touched: str


class ModuleC(Module):
    name = "C"
    title = "NY continuation pullback"
    window = "08:20–11:00 New York"

    def reset_day(self) -> None:
        super().reset_day()
        self.setup: _Setup | None = None
        self.ref: str | None = None
        self.bias: int | None = None
        self.status = "waiting for the New York window"

    def done(self) -> bool:
        return self.trades >= self.cfg.max_trades or self.losses >= self.cfg.max_losses

    def compute_bias(self, price: float) -> int | None:
        vwap = self.e.vwap.value
        slope = self.e.ema15.slope(self.cfg.slope_bars)
        if vwap is None or slope is None:
            return None
        if price > vwap and slope > 0:
            return LONG
        if price < vwap and slope < 0:
            return SHORT
        return None

    def _disarm(self, why: str) -> None:
        if self.ref:
            self.e.cancel(self.ref, f"setup dropped: {why}")
        self.setup = None
        self.status = f"setup dropped: {why}"

    def on_m5(self, bar: Bar) -> None:
        e, cfg, w = self.e, self.cfg, self.e.w
        if not (w.ny_start < bar.close_time <= w.ny_end):
            return
        if self.done():
            if self.setup:
                self._disarm("module done for the day")
            self.status = "done for the day" + (" (2 losses)" if self.losses >= cfg.max_losses else "")
            return
        self.bias = self.compute_bias(bar.c)
        vwap, ema = e.vwap.value, e.ema5.value
        self.detail = {
            "bias": side_name(self.bias) if self.bias else "none",
            "vwap": vwap, "ema_m5": ema, "ema_m15_slope": e.ema15.slope(cfg.slope_bars),
        }
        if self.setup and self.setup.side != self.bias:
            self._disarm("bias changed")
        if self.bias is None:
            if not self.setup:
                self.status = "no bias (VWAP and M15 EMA slope disagree)"
            return

        tol = cfg.touch_tolerance
        touched = None
        if self.bias == LONG:
            if ema is not None and bar.l <= ema + tol:
                touched = "EMA20"
            elif vwap is not None and bar.l <= vwap + tol:
                touched = "VWAP"
            extreme = bar.l
        else:
            if ema is not None and bar.h >= ema - tol:
                touched = "EMA20"
            elif vwap is not None and bar.h >= vwap - tol:
                touched = "VWAP"
            extreme = bar.h

        if touched:
            expires = bar.close_time + cfg.setup_expiry_bars * 300
            if self.setup is None:
                self.setup = _Setup(self.bias, extreme, expires, bar.t, touched)
            else:
                s = self.setup
                s.extreme = min(s.extreme, extreme) if s.side == LONG else max(s.extreme, extreme)
                s.expires = expires
            self.status = f"{side_name(self.bias)} pullback touched {touched}, waiting for the M1 break"
        elif self.setup and bar.close_time >= self.setup.expires:
            self._disarm("expired")
        elif not self.setup:
            self.status = f"{side_name(self.bias)} bias, waiting for a pullback to EMA20/VWAP"

    def _swing_level(self, s: _Setup) -> float | None:
        bars = [b for b in self.e.m1 if b.t >= s.start_ts - 15 * 60]
        k = self.cfg.swing_strength
        n = len(bars)
        for i in range(n - 1 - k, k - 1, -1):
            if s.side == LONG:
                x = bars[i].h
                if all(bars[j].h < x for j in range(i - k, i)) and all(bars[j].h <= x for j in range(i + 1, i + k + 1)):
                    return x
            else:
                x = bars[i].l
                if all(bars[j].l > x for j in range(i - k, i)) and all(bars[j].l >= x for j in range(i + 1, i + k + 1)):
                    return x
        tail = bars[-self.cfg.swing_fallback_bars:]
        if not tail:
            return None
        return max(b.h for b in tail) if s.side == LONG else min(b.l for b in tail)

    def on_m1(self, bar: Bar) -> None:
        e, cfg, w = self.e, self.cfg, self.e.w
        s = self.setup
        if s is None:
            return
        if e.now > w.ny_end or self.done():
            self._disarm("window closed" if e.now > w.ny_end else "module done")
            return
        if s.side == LONG:
            s.extreme = min(s.extreme, bar.l)
        else:
            s.extreme = max(s.extreme, bar.h)
        swing = self._swing_level(s)
        if swing is None:
            return
        ok, why = e.can_open(self.name)
        if not ok:
            if self.ref:
                e.cancel(self.ref, why)
            self.status = f"setup armed but blocked: {why}"
            return

        if s.side == LONG:
            entry = swing + cfg.entry_buffer
            stop = s.extreme - cfg.stop_buffer
            crossed = e.ask >= entry
            broke_this_bar = bar.o + bar.s < entry
        else:
            entry = swing - cfg.entry_buffer
            stop = s.extreme + cfg.stop_buffer
            crossed = e.bid <= entry
            broke_this_bar = bar.o > entry
        self.detail.update({"swing": swing, "trigger": entry, "pullback_extreme": s.extreme})

        if crossed:
            if not broke_this_bar:
                return  # price is already past this swing: wait for a fresh one
            if self.ref:
                e.cancel(self.ref, "replaced by a market entry")
            sig = self._signal(s.side, "market", e.ask if s.side == LONG else e.bid, stop)
            ref = e.submit(sig)
            if ref is None:
                self._disarm("entry skipped by the shared rules")
            return

        sig = self._signal(s.side, "stop", entry, stop)
        if self.ref and self.ref in e.orders:
            self.ref = e.amend_entry(self.ref, sig)
        else:
            self.ref = e.submit(sig)
            if self.ref:
                self.refs.add(self.ref)
        if self.ref:
            self.status = f"{side_name(s.side)} stop @ {entry:.2f} (swing {swing:.2f}), stop loss {stop:.2f}"

    def _signal(self, side: int, kind: str, entry: float, stop: float) -> Signal:
        return Signal(
            module=self.name, side=side, kind=kind, entry=entry, stop=stop, target_r=self.cfg.target_r,
            apply_level_rules=self.cfg.apply_level_rules, expiry_ts=self.e.w.ny_end if kind == "stop" else None,
            tag=f"pullback-{self.setup.touched if self.setup else ''}",
        )

    def on_fill(self, order: Order, position: Position) -> None:
        super().on_fill(order, position)
        self.ref = None
        self.setup = None
        self.status = f"{side_name(position.side)} filled @ {position.entry:.2f}"

    def on_cancel(self, order: Order, reason: str) -> None:
        super().on_cancel(order, reason)
        if order.ref == self.ref:
            self.ref = None

    def on_close(self, trade: ClosedTrade) -> None:
        super().on_close(trade)
        if self.done() and (self.setup or self.ref):
            # the second loss ends Module C's day at once, including a stop order already working
            self._disarm("module done for the day")
