import pytest

from gse.config import LevelSettings, RiskSettings
from gse.levels import adjust_stop, adjust_target, level_above, level_below
from gse.models import SymbolSpec
from gse.risk import Signal, Skip, TradePlanner

L = LevelSettings()


def test_next_levels_are_strict():
    assert level_above(2400.0) == 2420.0
    assert level_above(2399.99) == 2400.0
    assert level_below(2400.0) == 2380.0
    assert level_below(2400.01) == 2400.0


def test_target_pulled_one_dollar_short_of_next_level():
    assert adjust_target(1, 2405.0, 2430.0, L) == (2419.0, True)
    assert adjust_target(1, 2405.0, 2415.0, L) == (2415.0, False)
    assert adjust_target(-1, 2405.0, 2390.0, L) == (2401.0, True)
    assert adjust_target(-1, 2405.0, 2402.0, L) == (2402.0, False)
    # a target exactly on the level is also moved
    assert adjust_target(1, 2405.0, 2420.0, L) == (2419.0, True)


def test_stop_pushed_beyond_level():
    # long: zone (L - 2.5, L + 2] is pushed to L - 2.5
    assert adjust_stop(1, 2410.0, 2401.5, L) == (2397.5, True)
    assert adjust_stop(1, 2410.0, 2399.0, L) == (2397.5, True)
    assert adjust_stop(1, 2410.0, 2397.5, L) == (2397.5, False)
    assert adjust_stop(1, 2410.0, 2402.5, L) == (2402.5, False)
    # short mirrored
    assert adjust_stop(-1, 2390.0, 2398.5, L) == (2402.5, True)
    assert adjust_stop(-1, 2390.0, 2401.0, L) == (2402.5, True)
    assert adjust_stop(-1, 2390.0, 2397.5, L) == (2397.5, False)
    # a level on the far side of the entry is ignored
    assert adjust_stop(1, 2399.0, 2395.0, L) == (2395.0, False)


def planner(**kw):
    sym = SymbolSpec(commission_per_unit_rt=0.07, **kw)
    return TradePlanner(RiskSettings(), LevelSettings(), sym)


def test_sizing_from_money_at_risk():
    p = planner()
    plan = p.plan(Signal("C", 1, "stop", 2405.0, 2401.0, target_r=2.0, apply_level_rules=False), 1.0, 100_000)
    # 0.3% of 100k = 300; per oz: 4 + 0.10 slippage + 0.07 commission = 4.17 -> 71 oz
    assert plan.units == 71
    assert plan.risk_money == pytest.approx(71 * 4.17)
    assert plan.tp == pytest.approx(2413.0)


def test_sizing_uses_account_currency():
    sym = SymbolSpec(commission_per_unit_rt=0.0, quote_to_account=0.5)  # 1 USD = 0.5 account ccy
    p = TradePlanner(RiskSettings(sizing_slippage_usd=0.0), LevelSettings(), sym)
    plan = p.plan(Signal("C", 1, "stop", 2405.0, 2400.0, target_r=2.0, apply_level_rules=False), 1.0, 10_000)
    assert plan.units == 12  # 30 / (5 * 0.5)


def test_minimum_stop_distance_and_atr():
    p = planner()
    plan = p.plan(Signal("C", 1, "stop", 2405.0, 2404.0, target_r=2.0, apply_level_rules=False), 4.5, 100_000)
    assert plan.sl == pytest.approx(2400.5)  # widened to 1 x ATR = 4.5
    assert any("min distance" in n for n in plan.notes)


def test_room_rule_skips_and_level_rules_toggle():
    p = planner()
    sig = Signal("A", 1, "stop", 2409.5, 2401.0, target_r=2.5)
    res = p.plan(sig, 1.0, 100_000)
    assert isinstance(res, Skip) and "room" in res  # target cut to 2419 = 1.12R
    sig.apply_level_rules = False
    plan = p.plan(sig, 1.0, 100_000)
    assert plan.tp == pytest.approx(2430.75)


def test_too_small_position_is_skipped():
    p = planner(min_units=1.0)
    res = p.plan(Signal("A", 1, "stop", 2405.0, 2385.0, target_r=1.5, apply_level_rules=False), 1.0, 100)
    assert isinstance(res, Skip) and "minimum" in res


def test_partial_and_breakeven_plan():
    p = planner()
    plan = p.plan(Signal("B", -1, "market", 2405.0, 2409.0, target_mode="price", target_price=2393.0,
                         partial_price=2399.0, partial_fraction=0.5, be_after_partial=True, apply_level_rules=False),
                  1.0, 100_000)
    assert plan.partial_price == 2399.0 and plan.be_trigger == 2399.0 and plan.tp == 2393.0
    bad = p.plan(Signal("B", -1, "market", 2405.0, 2409.0, target_mode="price", target_price=2393.0,
                        partial_price=2410.0, partial_fraction=0.5, apply_level_rules=False), 1.0, 100_000)
    assert bad.partial_price is None


def test_nearer_target_mode():
    p = planner()
    plan = p.plan(Signal("D", -1, "market", 2410.0, 2413.0, target_r=1.5, target_price=2406.0, target_mode="nearer",
                         apply_level_rules=False), 1.0, 100_000)
    assert plan.tp == pytest.approx(2406.0)  # VWAP (1.33R) nearer than 1.5R (2405.5)
    res = p.plan(Signal("D", -1, "market", 2410.0, 2413.0, target_r=1.5, target_price=2407.0, target_mode="nearer",
                        apply_level_rules=False), 1.0, 100_000)
    assert isinstance(res, Skip)  # VWAP only 1R away: under the 1.2R minimum room
    plan = p.plan(Signal("D", -1, "market", 2410.0, 2414.0, target_r=1.5, target_price=2415.0, target_mode="nearer",
                         apply_level_rules=False), 1.0, 100_000)
    assert plan.tp == pytest.approx(2404.0)  # VWAP on the wrong side is ignored
