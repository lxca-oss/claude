from datetime import date

import numpy as np
import pytest

from gse.backtest import data as store
from gse.backtest.report import run_full
from gse.backtest.ticks import available_days, load_quotes, run_tick_backtest, save_day, synthetic_ticks
from gse.config import Settings
from gse.sessions import SessionClock, trading_date


def test_quote_merge_carries_each_side_forward():
    save_day("q", date(2024, 1, 2), [(1000, 10.0), (3000, 11.0)], [(2000, 10.4), (4000, 11.3)])
    ts, bid, ask = load_quotes("q", date(2024, 1, 2))
    assert ts.tolist() == [2000, 3000, 4000]  # nothing until both sides exist
    assert bid.tolist() == [10.0, 11.0, 11.0] and ask.tolist() == [10.4, 11.0, 11.3]  # ask never below bid
    assert available_days("q") == [date(2024, 1, 2)]


def test_tick_replay_backtest_runs_and_respects_the_rules():
    synthetic_ticks("tk", date(2024, 1, 8), date(2024, 1, 26), seed=4)
    assert store.list_datasets()[0]["tick_days"] == 15
    s = Settings()
    s.news.enabled = False
    res = run_tick_backtest(s, "tk", None, None)
    assert res["fill_model"] == "ticks" and res["bars"] > 500_000
    clock = SessionClock(s.sessions)
    for t in res["trades"]:
        w = clock.windows(trading_date(t["open_ts"]))
        assert t["close_ts"] <= w.flat + 60
        if t["exit_reason"] == "sl" and not t["be_moved"]:
            assert t["r"] < -0.9  # a full stop costs about 1R plus costs
    total = sum(t["pnl"] for t in res["trades"])
    assert res["final_balance"] == pytest.approx(res["initial_balance"] + total)


def test_run_full_with_tick_fill_model():
    synthetic_ticks("tk2", date(2024, 1, 8), date(2024, 1, 12), seed=5)
    rep = run_full({"dataset": "tk2", "fill_model": "ticks", "random_iterations": 0, "use_news": False}, None)
    assert rep["fill_model"] == "ticks"
    assert not any("assumed path" in w for w in rep["warnings"])
    rep = run_full({"dataset": "tk2", "random_iterations": 0, "use_news": False}, None)
    assert rep["fill_model"] == "bars" and any("assumed path" in w for w in rep["warnings"])
