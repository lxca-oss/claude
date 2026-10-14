"""Download history from cTrader into the local dataset store.

* ``trendbars``: M1 bid bars, fast; spread is unknown (the backtest uses the fixed spread).
* ``ticks``: bid and ask ticks aggregated to M1 bars with the real average spread of each
  minute. Slower (rate-limited to 5 history requests per second) but this is what
  validation gate 2 asks for: cTrader tick data with real spread.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np

from ...backtest import data as store
from .proto import model
from .session import CTraderSession

Progress = Callable[[float, str], None]


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def ticks_to_m1(bid: list[tuple[int, float]], ask: list[tuple[int, float]]) -> store.BarArrays:
    if not bid:
        return store.BarArrays.empty()
    bt = np.array([x[0] for x in bid], dtype=np.int64)
    bp = np.array([x[1] for x in bid], dtype=np.float64)
    sec = bt // 1000
    minute = sec - sec % 60
    starts = np.flatnonzero(np.r_[True, minute[1:] != minute[:-1]])
    o = bp[starts]
    c = bp[np.r_[starts[1:] - 1, len(bp) - 1]]
    h = np.maximum.reduceat(bp, starts)
    l = np.minimum.reduceat(bp, starts)
    counts = np.diff(np.r_[starts, len(bp)])
    spread = np.zeros(len(starts))
    if ask:
        at = np.array([x[0] for x in ask], dtype=np.int64)
        ap = np.array([x[1] for x in ask], dtype=np.float64)
        idx = np.searchsorted(at, bt, "right") - 1
        valid = idx >= 0
        sp = np.where(valid, ap[np.clip(idx, 0, None)] - bp, np.nan)
        sp = np.where(sp > 0, sp, np.nan)
        sums = np.add.reduceat(np.nan_to_num(sp), starts)
        ns = np.add.reduceat((~np.isnan(sp)).astype(np.int64), starts)
        spread = np.where(ns > 0, sums / np.maximum(ns, 1), 0.0)
    return store.BarArrays(t=minute[starts], o=o, h=h, l=l, c=c, v=counts.astype(np.float64), s=np.round(spread, 4))


async def download_trendbars(session: CTraderSession, symbol_id: int, start: date, end: date, dataset_id: str,
                             symbol_name: str, progress: Progress | None = None) -> dict:
    days = (end - start).days + 1
    chunk = 7
    meta = None
    d = start
    while d <= end:
        stop = min(d + timedelta(days=chunk), end + timedelta(days=1))
        bars = await session.trendbars(symbol_id, _ms(d), _ms(stop), model.M1)
        if bars:
            arr = store.BarArrays.from_columns({
                "t": [b.t for b in bars], "o": [b.o for b in bars], "h": [b.h for b in bars],
                "l": [b.l for b in bars], "c": [b.c for b in bars], "v": [b.v for b in bars],
            })
            meta = store.save_dataset(dataset_id, arr, source="ctrader-trendbars", symbol=symbol_name,
                                      notes="cTrader M1 trendbars (bid); no spread")
        if progress:
            progress(min(1.0, ((stop - start).days) / days), f"{d.isoformat()}: {len(bars)} bars")
        d = stop
    return meta or {}


async def download_ticks(session: CTraderSession, symbol_id: int, start: date, end: date, dataset_id: str,
                         symbol_name: str, progress: Progress | None = None, keep_raw: bool = True) -> dict:
    """Ticks aggregated to M1 (for bar backtests) and, with ``keep_raw``, the raw ticks for tick replay."""
    from ...backtest.ticks import save_day

    days = (end - start).days + 1
    meta = None
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() == 5:  # Saturday: market closed
            continue
        lo, hi = _ms(d), _ms(d + timedelta(days=1)) - 1
        bid = await session.ticks(symbol_id, model.BID, lo, hi)
        ask = await session.ticks(symbol_id, model.ASK, lo, hi) if bid else []
        arr = ticks_to_m1(bid, ask)
        if keep_raw and bid and ask:
            save_day(dataset_id, d, bid, ask)
        if len(arr):
            meta = store.save_dataset(dataset_id, arr, source="ctrader-ticks", symbol=symbol_name,
                                      notes="cTrader ticks aggregated to M1 bid bars with real spread")
        if progress:
            progress((i + 1) / days, f"{d.isoformat()}: {len(bid)} bid / {len(ask)} ask ticks")
    return meta or {}
