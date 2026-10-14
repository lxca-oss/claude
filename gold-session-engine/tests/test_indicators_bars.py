import pytest

from gse.bars import Aggregator, Bar
from gse.indicators import ATR, EMA, AnchoredVWAP


def test_ema_seed_and_update():
    e = EMA(3)
    assert e.update(1) is None and e.update(2) is None
    assert e.update(3) == pytest.approx(2.0)
    assert e.update(4) == pytest.approx(3.0)  # 2 + 0.5 * (4 - 2)
    assert e.slope(1) == pytest.approx(1.0)


def test_atr_wilder():
    a = ATR(3)
    for h, l, c in [(10, 8, 9), (11, 9, 10), (12, 10, 11)]:
        a.update(h, l, c)
    assert a.value == pytest.approx(2.0)
    a.update(15, 11, 14)  # TR = max(4, 4, 0) = 4
    assert a.value == pytest.approx((2 * 2 + 4) / 3)


def test_vwap_weights_by_volume():
    v = AnchoredVWAP()
    v.reset(0)
    v.update(3, 3, 3, 1)
    v.update(6, 6, 6, 2)
    assert v.value == pytest.approx(5.0)
    v.reset(100)
    assert v.value is None


def test_aggregator_emits_on_last_minute_and_on_gaps():
    agg = Aggregator(300)
    out = []
    for i in range(5):
        out += agg.update(Bar(600 + 60 * i, 10 + i, 11 + i, 9 + i, 10.5 + i, 1, 0.2))
    assert len(out) == 1
    b = out[0]
    assert (b.t, b.o, b.h, b.l, b.c, b.v) == (600, 10, 15, 9, 14.5, 5)
    assert b.s == pytest.approx(0.2)
    # a bucket with missing minutes is closed when the next bucket starts
    assert agg.update(Bar(900, 1, 2, 0.5, 1.5)) == []
    out = agg.update(Bar(1260, 3, 4, 2, 3))
    assert len(out) == 1 and out[0].t == 900
