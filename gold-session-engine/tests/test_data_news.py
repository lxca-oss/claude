import json
import lzma
import struct
from datetime import date, datetime, timezone

import numpy as np
import pytest

from gse.backtest import data as store
from gse.backtest.dukascopy import combine, detect_divisor, parse_bi5_candles
from gse.broker.ctrader.history import ticks_to_m1
from gse.config import NewsSettings
from gse.news import NewsCalendar, NewsEvent, parse_csv, parse_forex_factory


def T(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def test_csv_with_header_and_ask_column():
    text = "time,open,high,low,close,volume,ask_close\n" \
           "2024-03-05 07:00:00,2100.10,2101.00,2099.90,2100.50,12,2100.80\n" \
           "2024-03-05 07:01:00,2100.50,2100.90,2100.00,2100.20,9,2100.45\n"
    b = store.parse_csv_bars(text)
    assert len(b) == 2 and int(b.t[0]) == T("2024-03-05 07:00:00")
    assert b.s[0] == pytest.approx(0.30) and b.v[1] == 9


def test_metatrader_export_without_header_and_timezone_shift():
    text = "2024.03.05,09:00,2100.1,2101.0,2099.9,2100.5,12\n2024.03.05,09:01,2100.5,2100.9,2100.0,2100.2,9\n"
    b = store.parse_csv_bars(text, tz_offset_hours=2)  # server time UTC+2
    assert int(b.t[0]) == T("2024-03-05 07:00:00")


def test_dataset_roundtrip_merge_and_slice():
    a = store.generate_synthetic(date(2023, 12, 28), date(2024, 1, 3), seed=1)
    store.save_dataset("t1", a.slice(None, T("2024-01-01 00:00:00")), source="test")
    meta = store.save_dataset("t1", a.slice(T("2023-12-31 00:00:00"), None), source="test")  # overlapping merge
    assert meta["bars"] == len(a)
    back = store.load_dataset("t1", date(2024, 1, 2), date(2024, 1, 2))
    assert int(back.t[0]) >= T("2024-01-02 00:00:00") and int(back.t[-1]) < T("2024-01-03 00:00:00")
    assert [d["id"] for d in store.list_datasets()] == ["t1"]
    store.delete_dataset("t1")
    assert store.list_datasets() == []


def test_synthetic_data_is_sane():
    b = store.generate_synthetic(date(2024, 1, 1), date(2024, 1, 31), seed=3)
    assert np.all(b.h >= np.maximum(b.o, b.c)) and np.all(b.l <= np.minimum(b.o, b.c))
    assert np.all(np.diff(b.t) > 0)
    dow = ((b.t // 86400) + 4) % 7
    assert not np.any(dow == 5)  # no Saturday bars


def test_ticks_to_m1_with_real_spread():
    base = T("2024-03-05 07:00:00") * 1000
    bid = [(base + 1000, 2100.0), (base + 30_000, 2101.0), (base + 59_000, 2100.5), (base + 61_000, 2100.2)]
    ask = [(base, 2100.3), (base + 29_000, 2101.2), (base + 60_500, 2100.6)]
    b = ticks_to_m1(bid, ask)
    assert len(b) == 2
    assert (b.o[0], b.h[0], b.l[0], b.c[0]) == (2100.0, 2101.0, 2100.0, 2100.5)
    assert b.s[0] == pytest.approx(np.mean([0.3, 0.2, 0.7]), abs=1e-4)
    assert b.v[0] == 3 and b.s[1] == pytest.approx(0.4)


def test_dukascopy_bi5_parsing():
    day = date(2024, 3, 5)
    recs = [(0, 2100100, 2100500, 2099900, 2101000, 3.5), (60, 2100500, 2100200, 2100000, 2100900, 2.0)]
    raw = lzma.compress(b"".join(struct.pack(">5if", *r) for r in recs), format=lzma.FORMAT_ALONE)
    bid = parse_bi5_candles(raw, day)
    assert bid[1][0] == T("2024-03-05 00:01:00")
    assert detect_divisor(bid[0][4]) == 1000.0
    ask = [(t, o + 300, h + 300, l + 300, c + 300, v) for t, o, h, l, c, v in bid]
    b = combine(bid, ask)
    assert b.o[0] == pytest.approx(2100.1) and b.h[0] == pytest.approx(2101.0) and b.c[0] == pytest.approx(2100.5)
    assert b.s[0] == pytest.approx(0.3)


# ------------------------------------------------------------------------------- news
def test_forex_factory_feed_parsing():
    feed = json.dumps([
        {"title": "CPI m/m", "country": "USD", "date": "2026-10-15T08:30:00-04:00", "impact": "High"},
        {"title": "German ZEW", "country": "EUR", "date": "2026-10-15T05:00:00-04:00", "impact": "Medium"},
    ])
    ev = parse_forex_factory(feed)
    assert ev[0].ts == T("2026-10-15 12:30:00") and ev[0].currency == "USD" and ev[0].impact == "High"


def test_blackout_windows():
    cal = NewsCalendar(NewsSettings(), [
        NewsEvent(T("2026-10-15 12:30:00"), "USD", "High", "CPI m/m"),
        NewsEvent(T("2026-10-16 12:30:00"), "USD", "High", "Retail Sales m/m"),
        NewsEvent(T("2026-10-16 09:00:00"), "EUR", "High", "ECB Press Conference"),
        NewsEvent(T("2026-10-16 14:00:00"), "USD", "Medium", "Business Inventories"),
    ])
    cpi = T("2026-10-15 12:30:00")
    assert cal.blackout(cpi - 301) is None
    assert cal.blackout(cpi - 300).title == "CPI m/m"
    assert cal.blackout(cpi + 14 * 60).title == "CPI m/m"  # 15 minutes after CPI
    assert cal.blackout(cpi + 15 * 60) is None
    rs = T("2026-10-16 12:30:00")
    assert cal.blackout(rs + 4 * 60) is not None and cal.blackout(rs + 5 * 60) is None  # 5 minutes otherwise
    assert cal.blackout(T("2026-10-16 09:00:00")) is None  # EUR ignored
    assert cal.blackout(T("2026-10-16 14:00:00")) is None  # medium impact ignored
    assert cal.next_event(cpi + 3600).title == "Retail Sales m/m"


def test_news_csv_roundtrip_and_disabled_filter():
    text = "datetime_utc,currency,impact,title\n2026-11-06 13:30,USD,High,Non-Farm Employment Change\n"
    ev = parse_csv(text)
    assert ev[0].ts == T("2026-11-06 13:30:00")
    cal = NewsCalendar(NewsSettings(enabled=False), ev)
    assert cal.blackout(ev[0].ts) is None
