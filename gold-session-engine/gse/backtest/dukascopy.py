"""Download XAU/USD M1 history from Dukascopy's public datafeed.

FTMO's cTrader server may not hold tick history back to 2018 (an open question in the
strategy document), so this is an independent source for the two-halves test. Dukascopy
publishes daily M1 bid and ask candle files (LZMA-compressed, big-endian records of
time offset, open, close, low, high as integers and volume as a float). Downloading
both sides gives a real per-minute spread.

Dukascopy's spread and prices are not FTMO's: treat this data as a second broker, as
the London-breakout research did, and confirm on cTrader data where it overlaps.
"""

from __future__ import annotations

import asyncio
import lzma
import struct
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np

from . import data as store

URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m:02d}/{d:02d}/{side}_candles_min_1.bi5"
RECORD = struct.Struct(">5if")


def parse_bi5_candles(raw: bytes, day: date) -> list[tuple]:
    """(ts, open, high, low, close, volume) with raw integer prices."""
    if not raw:
        return []
    data = lzma.decompress(raw)
    base = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    out = []
    for i in range(0, len(data) - RECORD.size + 1, RECORD.size):
        t, o, c, l, h, v = RECORD.unpack_from(data, i)
        out.append((base + t, o, h, l, c, v))
    return out


def detect_divisor(sample_price: int) -> float:
    """Dukascopy stores XAU/USD with 3 decimals; detect it from the magnitude to be safe."""
    for div in (1000.0, 100.0, 100_000.0, 10.0, 1.0):
        if 100 <= sample_price / div <= 20_000:
            return div
    return 1000.0


def combine(bid: list[tuple], ask: list[tuple]) -> store.BarArrays:
    if not bid:
        return store.BarArrays.empty()
    div = detect_divisor(bid[0][4])
    ask_close = {r[0]: r[4] for r in ask}
    rows = [r for r in bid if r[5] > 0 or r[1] != r[4]]  # drop flat zero-volume filler minutes
    t = np.array([r[0] for r in rows], dtype=np.int64)
    o, h, l, c = (np.array([r[k] for r in rows], dtype=np.float64) / div for k in (1, 2, 3, 4))
    v = np.array([r[5] for r in rows], dtype=np.float64)
    s = np.array([max((ask_close.get(r[0], r[4]) - r[4]) / div, 0.0) for r in rows])
    return store.BarArrays(t=t, o=o, h=h, l=l, c=c, v=v, s=np.round(s, 4))


async def download(start: date, end: date, dataset_id: str = "dukascopy-XAUUSD", symbol: str = "XAUUSD",
                   progress: Callable[[float, str], None] | None = None, concurrency: int = 4) -> dict:
    import httpx

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() != 5]
    sem = asyncio.Semaphore(concurrency)
    meta = {}
    done = 0

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "gold-session-engine"}) as client:

        async def fetch(d: date, side: str) -> bytes:
            url = URL.format(sym=symbol, y=d.year, m=d.month - 1, d=d.day, side=side)
            async with sem:
                for attempt in range(4):
                    try:
                        r = await client.get(url)
                        if r.status_code == 404:
                            return b""
                        r.raise_for_status()
                        return r.content
                    except httpx.HTTPError:
                        await asyncio.sleep(1 + attempt * 2)
            raise RuntimeError(f"could not download {url}")

        batch: list[store.BarArrays] = []
        for d in days:
            bid_raw, ask_raw = await asyncio.gather(fetch(d, "BID"), fetch(d, "ASK"))
            arr = combine(parse_bi5_candles(bid_raw, d), parse_bi5_candles(ask_raw, d))
            if len(arr):
                batch.append(arr)
            done += 1
            if len(batch) >= 30 or d == days[-1]:
                if batch:
                    merged = batch[0]
                    for b in batch[1:]:
                        merged = merged.concat(b)
                    meta = store.save_dataset(dataset_id, merged, source="dukascopy", symbol=symbol,
                                              notes="Dukascopy public datafeed, M1 bid with ask-close spread")
                    batch = []
            if progress:
                progress(done / len(days), d.isoformat())
    return meta
