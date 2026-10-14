from datetime import date

import numpy as np
import pytest

from gse.backtest import data as store
from gse.backtest.baseline import simulate_twin
from gse.backtest.data import BarArrays
from gse.backtest.report import run_full
from gse.backtest.stats import trade_stats


def bars_from(closes, spread=0.0):
    c = np.array(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return BarArrays(t=np.arange(len(c)) * 60, o=o, h=np.maximum(o, c) + 0.1, l=np.minimum(o, c) - 0.1, c=c,
                     v=np.ones(len(c)), s=np.full(len(c), spread))


def test_twin_long_hits_target():
    b = bars_from([100, 100, 101, 102, 103, 104, 105, 106])
    r = simulate_twin(b, 0, 7, 1, d=2.0, tgt=4.0, l1=None, frac=0, has_partial=False, slip=0, comm_rt=0,
                      sizing_slip=0, spread_default=0.0)
    assert r == pytest.approx(2.0)


def test_twin_short_stopped_and_breakeven():
    b = bars_from([100, 100.5, 101, 102.5, 103])
    r = simulate_twin(b, 0, 4, -1, d=2.0, tgt=4.0, l1=None, frac=0, has_partial=False, slip=0, comm_rt=0,
                      sizing_slip=0, spread_default=0.0)
    assert r == pytest.approx(-1.0)
    b = bars_from([100, 101, 102, 103.2, 102, 100.5, 99])
    r = simulate_twin(b, 0, 6, 1, d=2.0, tgt=6.0, l1=103.0, frac=0, has_partial=False, slip=0, comm_rt=0,
                      sizing_slip=0, spread_default=0.0)
    assert r == pytest.approx(0.0)  # break-even at +1.5R, then back to entry


def test_trade_stats():
    trades = [{"r": 2.0, "pnl": 200, "open_ts": 0, "close_ts": 600}, {"r": -1.0, "pnl": -100, "open_ts": 0, "close_ts": 60},
              {"r": -1.0, "pnl": -100, "open_ts": 0, "close_ts": 60}, {"r": 2.0, "pnl": 200, "open_ts": 0, "close_ts": 60}]
    s = trade_stats(trades)
    assert s["trades"] == 4 and s["win_rate"] == 0.5 and s["mean_r"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0) and s["max_losing_streak"] == 2
    assert s["breakeven_win_rate"] == pytest.approx(1 / 3)


def test_full_pipeline_with_baseline_and_gates():
    store.save_dataset("syn", store.generate_synthetic(date(2021, 10, 1), date(2022, 3, 31), seed=5), source="synthetic")
    rep = run_full({"dataset": "syn", "random_iterations": 20, "use_news": False}, lambda f, m: None)
    assert rep["kind"] == "single" and rep["summary"]["trades"] > 20
    assert {g["gate"] for g in rep["gates"]} >= {"Beats random entries", "Both halves positive", "Significance",
                                                   "Combined drawdown", "Forward test"}
    assert any("SYNTHETIC" in w for w in rep["warnings"])
    for m, b in rep["baseline"].items():
        if b.get("iterations"):
            assert 0 <= b["percentile"] <= 100 and len(b["histogram"]["counts"]) == 30
    assert rep["halves"]["first"]["all"]["trades"] > 0 and rep["halves"]["second"]["all"]["trades"] > 0
    # variants
    rep = run_full({"dataset": "syn", "random_iterations": 0, "use_news": False, "preset": "module_c_alone",
                    "variants": [{"name": "1.5R", "overrides": {"module_c.target_r": 1.5}},
                                 {"name": "2R", "overrides": {"module_c.target_r": 2.0}}]}, None)
    assert rep["kind"] == "variants" and len(rep["variants"]) == 2
    assert set(rep["variants"][0]["modules"]) == {"C"}
