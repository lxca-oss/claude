"""Bars and timeframe aggregation.

The engine is driven by closed M1 bid bars. Each bar also carries the average spread
for that minute (from tick data, ask bars, or a fixed assumption), which the simulator
uses to derive ask prices. M5 and M15 bars are built from M1 bars aligned to UTC.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Bar:
    t: int  # open time, unix seconds (UTC)
    o: float  # bid prices
    h: float
    l: float
    c: float
    v: float = 0.0  # tick volume
    s: float = 0.0  # spread (ask - bid), $
    tf: int = 60  # seconds

    @property
    def close_time(self) -> int:
        return self.t + self.tf

    def to_dict(self) -> dict:
        return {"t": self.t, "o": self.o, "h": self.h, "l": self.l, "c": self.c, "v": self.v, "s": self.s}


class Aggregator:
    """Builds higher-timeframe bars from M1 bars.

    A bar is emitted as soon as its last minute arrives. If minutes are missing (thin
    markets, reconnects) the bar is emitted when the first minute of a later bucket
    arrives instead.
    """

    def __init__(self, tf_seconds: int):
        self.tf = tf_seconds
        self.cur: Bar | None = None
        self._spread_sum = 0.0
        self._n = 0

    def update(self, m1: Bar) -> list[Bar]:
        out: list[Bar] = []
        bucket = m1.t - m1.t % self.tf
        if self.cur is not None and bucket != self.cur.t:
            out.append(self._finish())
        if self.cur is None:
            self.cur = Bar(bucket, m1.o, m1.h, m1.l, m1.c, m1.v, m1.s, self.tf)
            self._spread_sum, self._n = m1.s, 1
        else:
            c = self.cur
            if m1.h > c.h:
                c.h = m1.h
            if m1.l < c.l:
                c.l = m1.l
            c.c = m1.c
            c.v += m1.v
            self._spread_sum += m1.s
            self._n += 1
        if m1.t + 60 >= bucket + self.tf:
            out.append(self._finish())
        return out

    def _finish(self) -> Bar:
        bar = self.cur
        assert bar is not None
        bar.s = self._spread_sum / max(self._n, 1)
        self.cur = None
        return bar
