"""Session clock across daylight-saving switches (build note: unit-test the DST weeks)."""

from datetime import date, datetime, timezone

from gse.config import SessionSettings
from gse.sessions import SessionClock, trading_date


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


clock = SessionClock(SessionSettings())


def test_winter_both_standard_time():
    w = clock.windows(date(2026, 1, 14))  # GMT and EST
    assert utc(w.asian_start) == "2026-01-14 00:00"
    assert utc(w.london_start) == "2026-01-14 07:00"
    assert utc(w.ny_start) == "2026-01-14 13:20"
    assert utc(w.pmfix_start) == "2026-01-14 14:45"
    assert utc(w.flat) == "2026-01-14 16:30"


def test_summer_both_daylight_time():
    w = clock.windows(date(2026, 7, 15))  # BST and EDT
    assert utc(w.asian_start) == "2026-07-14 23:00"
    assert utc(w.london_start) == "2026-07-15 06:00"
    assert utc(w.ny_start) == "2026-07-15 12:20"
    assert utc(w.flat) == "2026-07-15 15:30"


def test_autumn_mixed_week_london_moves_first():
    # London leaves BST on 25 Oct 2026, New York leaves EDT on 1 Nov 2026
    w = clock.windows(date(2026, 10, 28))
    assert utc(w.london_start) == "2026-10-28 07:00"  # London rows shift +1h in UTC
    assert utc(w.ny_start) == "2026-10-28 12:20"  # New York rows do not
    w2 = clock.windows(date(2026, 11, 4))
    assert utc(w2.ny_start) == "2026-11-04 13:20"


def test_spring_mixed_week_new_york_moves_first():
    # US DST starts 8 Mar 2026, UK on 29 Mar 2026
    w = clock.windows(date(2026, 3, 18))
    assert utc(w.london_start) == "2026-03-18 07:00"
    assert utc(w.ny_start) == "2026-03-18 12:20"
    w2 = clock.windows(date(2026, 3, 31))
    assert utc(w2.london_start) == "2026-03-31 06:00"


def test_sydney_times_match_the_strategy_document():
    rows = {r["window"]: r for r in clock.windows(date(2026, 10, 14)).rows()}  # AEDT, BST, EDT
    assert rows["Asian range"]["sydney"] == "10:00–17:00"
    assert rows["Module A + B (London)"]["sydney"] == "17:00–20:00"
    assert rows["Module C (New York)"]["sydney"] == "23:20–02:00"
    assert rows["Module D (PM fix)"]["sydney"] == "00:45–01:30"
    rows = {r["window"]: r for r in clock.windows(date(2026, 11, 4)).rows()}  # from 2 Nov
    assert rows["Asian range"]["sydney"] == "11:00–18:00"
    assert rows["Module A + B (London)"]["sydney"] == "18:00–21:00"
    assert rows["Module C (New York)"]["sydney"] == "00:20–03:00"
    assert rows["No new trades / flat"]["sydney"] == "03:00–03:30"


def test_trading_date_is_the_london_date():
    summer_late = int(datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc).timestamp())
    assert trading_date(summer_late) == date(2026, 7, 16)  # 00:30 BST
    winter_late = int(datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc).timestamp())
    assert trading_date(winter_late) == date(2026, 1, 15)


def test_weekend_flag():
    assert clock.windows(date(2026, 10, 17)).is_weekend
    assert not clock.windows(date(2026, 10, 16)).is_weekend
