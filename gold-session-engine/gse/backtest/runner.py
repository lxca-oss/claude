"""Backtest runner: replays M1 bars through the real engine and the simulator."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from ..config import Settings
from ..engine import StrategyEngine
from ..models import SymbolSpec
from ..news import NewsCalendar
from ..broker.sim import SimBroker
from .data import BarArrays

ProgressFn = Callable[[float, str], None]


def symbol_from_costs(settings: Settings) -> SymbolSpec:
    c = settings.costs
    return SymbolSpec(
        name=settings.ctrader.symbol,
        digits=2,
        contract_size=c.contract_size_oz,
        min_units=round(c.min_lots * c.contract_size_oz, 8),
        step_units=round(c.lot_step * c.contract_size_oz, 8),
        commission_per_unit_rt=c.commission_per_oz_rt,
    )


def run_backtest(
    settings: Settings,
    bars: BarArrays,
    news: NewsCalendar | None = None,
    progress: ProgressFn | None = None,
    record_events: bool = False,
) -> dict:
    """Run one backtest and return the raw results (trades, daily equity, skip counts)."""
    t_start = time.time()
    symbol = symbol_from_costs(settings)
    sim = SimBroker(symbol, settings.costs, settings.backtest.initial_balance)
    news = news if news is not None else NewsCalendar(settings.news)
    engine = StrategyEngine(settings, sim, news, record_events=record_events)

    days: list[dict] = []
    cur_day = None
    day_open_balance = sim.balance()
    day_min_eq = day_max_eq = prev_close_balance = sim.balance()
    n = len(bars)
    step = max(1, n // 200)

    for i, bar in enumerate(bars.iter_bars()):
        sim.process_bar(bar)
        engine.on_bar(bar)
        if engine.day != cur_day:
            if cur_day is not None:
                days.append(_day_row(cur_day, day_open_balance, day_min_eq, day_max_eq, prev_close_balance))
            cur_day = engine.day
            day_open_balance = engine.daily.start_balance
            day_min_eq = day_max_eq = day_open_balance
        if sim.positions:
            eq = sim.equity()
            if eq < day_min_eq:
                day_min_eq = eq
            if eq > day_max_eq:
                day_max_eq = eq
        bal = sim.balance()
        if bal < day_min_eq:
            day_min_eq = bal
        if bal > day_max_eq:
            day_max_eq = bal
        prev_close_balance = bal
        if progress and i % step == 0:
            progress(i / n, f"{datetime.fromtimestamp(bar.t, timezone.utc):%Y-%m-%d}")

    # close anything still open at the end of the data
    engine.flatten_all("end of data")
    if cur_day is not None:
        days.append(_day_row(cur_day, day_open_balance, day_min_eq, day_max_eq, sim.balance()))

    return {
        "trades": [t.to_dict() for t in engine.closed],
        "days": days,
        "skips": dict(engine.skips),
        "final_balance": sim.balance(),
        "initial_balance": settings.backtest.initial_balance,
        "bars": n,
        "start_ts": int(bars.t[0]) if n else None,
        "end_ts": int(bars.t[-1]) if n else None,
        "elapsed_s": time.time() - t_start,
        "events": list(engine.events) if record_events else [],
    }


def _day_row(day, open_balance, min_eq, max_eq, close_balance) -> dict:
    return {
        "day": day.isoformat(),
        "open_balance": open_balance,
        "close_balance": close_balance,
        "min_equity": min_eq,
        "max_equity": max_eq,
        "pnl": close_balance - open_balance,
    }
