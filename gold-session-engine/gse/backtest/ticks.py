"""Tick replay backtests.

M1-bar backtests have to assume a price path inside each minute. Module C (a few-dollar
stops, M1 triggers) and Module B are sensitive to that assumption, so validation gate 2
asks for tick data. When cTrader ticks are downloaded the raw bid/ask ticks are kept
under ``data/ticks/<dataset>/`` (one file per day). A tick backtest streams those quotes
into the simulator, so stop entries, stop losses, targets, break-even moves and partials
fire exactly when the quotes reach them, while the engine still sees the same closed M1
bars as in live trading.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .. import paths
from ..bars import Bar
from ..broker.sim import SimBroker
from ..config import Settings
from ..engine import StrategyEngine
from ..news import NewsCalendar


def tick_dir(dataset_id: str) -> Path:
    return paths.data_dir() / "ticks" / dataset_id


def save_day(dataset_id: str, day: date, bid: list[tuple[int, float]], ask: list[tuple[int, float]]) -> None:
    d = tick_dir(dataset_id)
    d.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        d / f"{day.isoformat()}.npz",
        bt=np.array([x[0] for x in bid], dtype=np.int64), bp=np.array([x[1] for x in bid], dtype=np.float64),
        at=np.array([x[0] for x in ask], dtype=np.int64), ap=np.array([x[1] for x in ask], dtype=np.float64),
    )


def available_days(dataset_id: str) -> list[date]:
    d = tick_dir(dataset_id)
    if not d.exists():
        return []
    return sorted(date.fromisoformat(f.stem) for f in d.glob("*.npz"))


def load_quotes(dataset_id: str, day: date) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Merged quote stream for one day: (timestamp ms, bid, ask), each side carried forward."""
    with np.load(tick_dir(dataset_id) / f"{day.isoformat()}.npz") as z:
        bt, bp, at, ap = z["bt"], z["bp"], z["at"], z["ap"]
    if len(bt) == 0 or len(at) == 0:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0)
    ts = np.union1d(bt, at)
    bi = np.searchsorted(bt, ts, "right") - 1
    ai = np.searchsorted(at, ts, "right") - 1
    ok = (bi >= 0) & (ai >= 0)
    ts, bi, ai = ts[ok], bi[ok], ai[ok]
    bid, ask = bp[bi], ap[ai]
    ask = np.maximum(ask, bid)
    return ts, bid, ask


def run_tick_backtest(settings: Settings, dataset_id: str, start: date | None, end: date | None,
                      news: NewsCalendar | None = None, progress=None) -> dict:
    from .runner import _day_row, symbol_from_costs

    t_start = time.time()
    days = [d for d in available_days(dataset_id) if (not start or d >= start) and (not end or d <= end)]
    if not days:
        raise ValueError(f"no raw ticks stored for {dataset_id}: download cTrader ticks first")
    sim = SimBroker(symbol_from_costs(settings), settings.costs, settings.backtest.initial_balance)
    engine = StrategyEngine(settings, sim, news if news is not None else NewsCalendar(settings.news),
                            record_events=False)

    rows: list[dict] = []
    cur_day = None
    day_open = day_min = day_max = prev_bal = sim.balance()
    n_quotes = 0
    first_ts = last_ts = None
    bar: Bar | None = None
    spreads = 0.0
    n_in_bar = 0

    def close_bar():
        nonlocal bar, cur_day, day_open, day_min, day_max, prev_bal
        bar.s = spreads / max(n_in_bar, 1)
        engine.on_bar(bar)
        if engine.day != cur_day:
            if cur_day is not None:
                rows.append(_day_row(cur_day, day_open, day_min, day_max, prev_bal))
            cur_day = engine.day
            day_open = day_min = day_max = engine.daily.start_balance
        bar = None

    for di, day in enumerate(days):
        ts, bid, ask = load_quotes(dataset_id, day)
        if len(ts) == 0:
            continue
        first_ts = first_ts if first_ts is not None else int(ts[0] // 1000)
        last_ts = int(ts[-1] // 1000)
        n_quotes += len(ts)
        for t_ms, b, a in zip(ts.tolist(), bid.tolist(), ask.tolist()):
            t = t_ms // 1000
            minute = t - t % 60
            if bar is not None and minute != bar.t:
                close_bar()
            sim.process_quote(t, b, a)
            engine.on_quote(t, b, a)
            if bar is None:
                bar = Bar(minute, b, b, b, b, 1.0, 0.0)
                spreads, n_in_bar = a - b, 1
            else:
                if b > bar.h:
                    bar.h = b
                if b < bar.l:
                    bar.l = b
                bar.c = b
                bar.v += 1
                spreads += a - b
                n_in_bar += 1
            if sim.positions:
                eq = sim.equity()
                day_min = min(day_min, eq)
                day_max = max(day_max, eq)
            bal = sim.balance()
            day_min = min(day_min, bal)
            day_max = max(day_max, bal)
            prev_bal = bal
        if progress:
            progress((di + 1) / len(days), day.isoformat())
    if bar is not None:
        close_bar()
    engine.flatten_all("end of data")
    if cur_day is not None:
        rows.append(_day_row(cur_day, day_open, day_min, day_max, sim.balance()))
    return {
        "trades": [t.to_dict() for t in engine.closed],
        "days": rows,
        "skips": dict(engine.skips),
        "final_balance": sim.balance(),
        "initial_balance": settings.backtest.initial_balance,
        "bars": n_quotes,
        "start_ts": first_ts,
        "end_ts": last_ts,
        "elapsed_s": time.time() - t_start,
        "events": [],
        "fill_model": "ticks",
    }


def synthetic_ticks(dataset_id: str, start: date, end: date, seed: int = 3, start_price: float = 2400.0) -> int:
    """Random-walk ticks every 2 seconds plus the matching M1 dataset (testing only). Returns days written."""
    from ..broker.ctrader.history import ticks_to_m1
    from . import data as store

    rng = np.random.default_rng(seed)
    price = start_price
    written = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            t0 = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) * 1000
            ts = np.arange(t0, t0 + 86_400_000, 2000, dtype=np.int64)
            steps = rng.normal(0, 0.06, len(ts))
            px = np.round(price + np.cumsum(steps), 2)
            price = float(px[-1])
            spread = np.round(0.2 + 0.1 * rng.random(len(ts)), 2)
            bid = list(zip(ts.tolist(), px.tolist()))
            ask = list(zip(ts.tolist(), (px + spread).tolist()))
            save_day(dataset_id, d, bid, ask)
            store.save_dataset(dataset_id, ticks_to_m1(bid, ask), source="synthetic", notes="synthetic ticks")
            written += 1
        d += timedelta(days=1)
    return written
