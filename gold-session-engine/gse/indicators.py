"""Streaming indicators: EMA, Wilder ATR and anchored VWAP."""

from __future__ import annotations

from collections import deque


class EMA:
    """Exponential moving average seeded with the simple average of the first ``period`` values."""

    def __init__(self, period: int, history: int = 64):
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []
        self.history: deque[float] = deque(maxlen=history)

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
                self._seed.clear()
        else:
            self.value += self.alpha * (x - self.value)
        if self.value is not None:
            self.history.append(self.value)
        return self.value

    def slope(self, bars: int) -> float | None:
        """value[now] - value[bars ago]."""
        if len(self.history) <= bars:
            return None
        return self.history[-1] - self.history[-1 - bars]


class ATR:
    """Average true range with Wilder smoothing."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    def update(self, h: float, l: float, c: float) -> float | None:
        if self._prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - self._prev_close), abs(l - self._prev_close))
        self._prev_close = c
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
                self._seed.clear()
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class AnchoredVWAP:
    """VWAP of typical price weighted by tick volume, reset at an anchor time.

    Bars with no volume information get weight 1 so the VWAP degrades to a time-weighted
    average instead of failing.
    """

    def __init__(self):
        self.anchor: int | None = None
        self._pv = 0.0
        self._vol = 0.0

    def reset(self, anchor_ts: int) -> None:
        self.anchor = anchor_ts
        self._pv = 0.0
        self._vol = 0.0

    def update(self, h: float, l: float, c: float, v: float) -> float | None:
        w = v if v > 0 else 1.0
        self._pv += (h + l + c) / 3.0 * w
        self._vol += w
        return self.value

    @property
    def value(self) -> float | None:
        return self._pv / self._vol if self._vol > 0 else None
