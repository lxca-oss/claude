"""Engine and module behaviour on hand-built price paths, plus rule invariants on long runs."""

import math
from collections import defaultdict
from datetime import date, datetime, timezone

import numpy as np
import pytest

from gse.backtest.data import BarArrays, generate_synthetic
from gse.backtest.runner import run_backtest
from gse.config import Settings
from gse.news import NewsCalendar, NewsEvent
from gse.risk import classify
from gse.sessions import SessionClock, trading_date

DAY = date(2026, 1, 14)  # Wednesday, London = UTC, New York = UTC-5


def ts(hhmm: str, day: date = DAY) -> int:
    h, m = map(int, hhmm.split(":"))
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc).timestamp())


def path_bars(segments, spread=0.2, wick=0.15, prev_day=True) -> BarArrays:
    """M1 bars along piecewise-linear price segments [(start 'HH:MM', end 'HH:MM', p0, p1), ...] on DAY.

    Gaps between segments are filled flat. A quiet previous day warms the indicators up.
    """
    cols = {k: [] for k in "tohlcvs"}

    def add(t, o, c):
        cols["t"].append(t)
        cols["o"].append(round(o, 2))
        cols["c"].append(round(c, 2))
        cols["h"].append(round(max(o, c) + wick, 2))
        cols["l"].append(round(min(o, c) - wick, 2))
        cols["v"].append(10.0)
        cols["s"].append(spread)

    first_price = segments[0][2]
    if prev_day:
        start = ts("00:00") - 86400 + 3600
        for i in range(0, 86400 - 3600, 60):
            p = first_price + 0.6 * math.sin(i / 600)
            add(start + i, p, first_price + 0.6 * math.sin((i + 60) / 600))
    t = ts("00:00")
    price = first_price
    for s, e, p0, p1 in segments:
        t0, t1 = ts(s), ts(e)
        while t < t0:
            add(t, price, price)
            t += 60
        n = max(1, (t1 - t0) // 60)
        for i in range(n):
            a = p0 + (p1 - p0) * i / n
            b = p0 + (p1 - p0) * (i + 1) / n
            add(t, a, b)
            t += 60
        price = p1
    while t < ts("21:00"):
        add(t, price, price)
        t += 60
    return BarArrays.from_columns(cols)


def asian_range(lo, hi):
    """Oscillate inside [lo, hi] from 00:00 to 07:00 London and touch both edges."""
    mid = (lo + hi) / 2
    segs = []
    pts = [mid, hi - 0.15, mid, lo + 0.15, mid, hi - 0.15, lo + 0.15, mid]
    times = ["00:00", "01:00", "02:00", "03:00", "04:00", "05:00", "06:00", "06:40", "07:00"]
    for i in range(len(pts) - 1):
        segs.append((times[i], times[i + 1], pts[i], pts[i + 1]))
    segs.append((times[-2], times[-1], pts[-1], mid))
    return segs


def settings(**mods) -> Settings:
    s = Settings()
    s.news.enabled = False
    for m in "abcd":
        getattr(s, f"module_{m}").enabled = m in mods.get("on", "")
    s.module_a.apply_level_rules = mods.get("levels", False)
    return s


def run(s, bars, news=None, events=False):
    return run_backtest(s, bars, news or NewsCalendar(s.news), record_events=events)


# ------------------------------------------------------------------------------- Module A
def a_day(after):
    return path_bars(asian_range(2401.0, 2409.0) + after)


def test_module_a_breakout_hits_target_and_cancels_other_side():
    bars = a_day([("07:00", "07:30", 2405.0, 2412.0), ("07:30", "09:00", 2412.0, 2433.0)])
    res = run(settings(on="a"), bars, events=True)
    trades = res["trades"]
    assert len(trades) == 1
    t = trades[0]
    assert t["module"] == "A" and t["side"] == 1 and t["exit_reason"] == "tp"
    assert t["planned_entry"] == pytest.approx(2409.5)
    assert t["sl_initial"] == pytest.approx(2401.0)
    assert t["tp"] == pytest.approx(2409.5 + 2.5 * 8.5)
    assert 2.2 < t["r"] < 2.5  # 2.5R less costs
    assert any("OCO" in e["message"] for e in res["events"])


def test_module_a_level_rule_conflict_skips_the_trade():
    bars = a_day([("07:00", "07:30", 2405.0, 2412.0), ("07:30", "09:00", 2412.0, 2433.0)])
    res = run(settings(on="a", levels=True), bars)
    assert res["trades"] == []
    assert any(k.startswith("A:") and "room" in k for k in res["skips"])


def test_module_a_range_filter():
    bars = path_bars(asian_range(2404.0, 2406.0) + [("07:00", "08:00", 2405.0, 2420.0)])  # 0.08% wide
    res = run(settings(on="a"), bars)
    assert res["trades"] == [] and res["skips"].get("A: range too narrow", 0) >= 1


def test_session_close_flattens_at_1130_new_york():
    bars = a_day([("07:00", "07:30", 2405.0, 2412.0), ("07:30", "09:00", 2412.0, 2420.0)])
    res = run(settings(on="a"), bars)
    t = res["trades"][0]
    assert t["exit_reason"] == "session close 11:30 NY"
    assert t["close_ts"] == ts("16:30")  # 11:30 New York in January


def test_server_exit_mode_keeps_module_a_until_2100_server_time():
    bars = a_day([("07:00", "07:30", 2405.0, 2412.0), ("07:30", "09:00", 2412.0, 2420.0)])
    s = settings(on="a")
    s.module_a.exit_mode = "server_2100"
    t = run(s, bars)["trades"][0]
    assert t["exit_reason"] == "server close 21:00" and t["close_ts"] == ts("19:00")  # 14:00 NY


def test_news_blackout_pulls_orders_and_replaces_them():
    bars = a_day([("07:00", "07:35", 2405.0, 2406.0), ("07:35", "08:00", 2406.0, 2413.0), ("08:00", "09:30", 2413.0, 2433.0)])
    s = settings(on="a")
    s.news.enabled = True
    cal = NewsCalendar(s.news, [NewsEvent(ts("07:20"), "USD", "High", "Retail Sales m/m")])
    res = run(s, bars, cal, events=True)
    msgs = [e["message"] for e in res["events"]]
    assert any("cancelled: news: Retail Sales" in m for m in msgs)
    assert sum("A BUY stop" in m for m in msgs) == 2  # placed at 07:00 and again after the blackout
    assert len(res["trades"]) == 1 and res["trades"][0]["exit_reason"] == "tp"
    assert res["trades"][0]["open_ts"] >= ts("07:25")


def test_daily_loss_stop_and_one_trade_per_day_for_a():
    bars = a_day([("07:00", "07:30", 2405.0, 2410.5), ("07:30", "08:30", 2410.5, 2395.0)])
    s = settings(on="a")
    s.risk.risk_per_trade_pct = 1.0
    s.risk.daily_loss_stop_pct = 0.9
    res = run(s, bars, events=True)
    # equity crosses -0.9% before the -1R stop, so the daily stop flattens the trade first
    assert len(res["trades"]) == 1 and res["trades"][0]["exit_reason"].startswith("day stopped: daily loss stop")
    assert -1.0 < res["trades"][0]["r"] < -0.8
    assert any(e["kind"] == "risk" and "daily loss stop" in e["message"] for e in res["events"])


# ------------------------------------------------------------------------------- Module B
def b_day():
    return path_bars(asian_range(2400.5, 2419.5) + [
        ("07:00", "07:08", 2412.0, 2420.9),  # sweep at least $1 above 2419.5
        ("07:08", "07:14", 2420.9, 2416.0),  # M5 close back inside: reclaim
        ("07:14", "08:30", 2416.0, 2401.0),  # midpoint then the opposite edge
    ])


def test_module_b_sweep_reclaim_partial_and_target():
    res = run(settings(on="b"), b_day(), events=True)
    trades = res["trades"]
    assert len(trades) == 1
    t = trades[0]
    assert t["module"] == "B" and t["side"] == -1
    assert t["open_ts"] == ts("07:10")  # the sweep bar itself closed back inside: market entry on its close
    assert t["partial_taken"] and t["be_moved"] and t["exit_reason"] == "tp"
    assert t["partial_price"] == pytest.approx(2410.0)
    assert t["tp"] == pytest.approx(2401.5)
    assert t["sl_initial"] > 2421.0 + 1.5 - 0.01


def test_module_b_closes_an_opposite_module_a_trade_first():
    s = settings(on="ab")
    res = run(s, b_day(), events=True)
    a = [t for t in res["trades"] if t["module"] == "A"]
    b = [t for t in res["trades"] if t["module"] == "B"]
    assert a and b
    assert a[0]["side"] == 1 and a[0]["exit_reason"] == "opposite signal from module B"
    assert a[0]["close_ts"] <= b[0]["open_ts"]


# ------------------------------------------------------------------------------- invariants
@pytest.fixture(scope="module")
def long_run():
    s = Settings()
    s.news.enabled = False
    s.module_d.enabled = True
    bars = generate_synthetic(date(2024, 1, 1), date(2024, 8, 31), seed=11)
    return s, run_backtest(s, bars, NewsCalendar(s.news))


def test_invariants_windows_and_session_close(long_run):
    s, res = long_run
    clock = SessionClock(s.sessions)
    assert len(res["trades"]) > 100
    for t in res["trades"]:
        w = clock.windows(trading_date(t["open_ts"]))
        assert not w.is_weekend
        start, end = {
            "A": (w.london_start, w.london_end), "B": (w.london_start, w.london_end),
            "C": (w.ny_start, w.ny_end), "D": (w.pmfix_start, w.pmfix_end),
        }[t["module"]]
        assert start <= t["open_ts"] <= end, t
        assert t["open_ts"] <= w.no_new_trades
        assert t["close_ts"] <= w.flat, t


def test_invariants_daily_caps(long_run):
    s, res = long_run
    per_day = defaultdict(list)
    for t in res["trades"]:
        per_day[trading_date(t["open_ts"])].append(t)
    caps = {"A": s.module_a.max_trades, "B": 2 * s.module_b.max_per_side, "C": s.module_c.max_trades,
            "D": s.module_d.max_trades}
    for day, ts_ in per_day.items():
        assert len(ts_) <= s.risk.max_trades_per_day
        for m, cap in caps.items():
            assert sum(t["module"] == m for t in ts_) <= cap
        # Module C stops after 2 losses: no C trade opens once two C losses have closed
        c = [t for t in ts_ if t["module"] == "C"]
        for t in c:
            earlier_losses = sum(1 for x in c if x["close_ts"] < t["open_ts"]
                                 and classify(x["r"], s.risk.scratch_r) == "loss")
            assert earlier_losses < s.module_c.max_losses, t["ref"]


def test_invariants_one_direction_and_risk(long_run):
    s, res = long_run
    trades = sorted(res["trades"], key=lambda t: t["open_ts"])
    for i, a in enumerate(trades):
        for b in trades[i + 1:]:
            if b["open_ts"] >= a["close_ts"]:
                continue
            assert a["side"] == b["side"], (a["ref"], b["ref"])
    balance = res["initial_balance"]
    for t in sorted(trades, key=lambda t: t["close_ts"]):
        assert t["risk_money"] <= balance * s.risk.risk_per_trade_pct / 100 * 1.15
        balance += t["pnl"]
    assert balance == pytest.approx(res["final_balance"], abs=1e-6)
