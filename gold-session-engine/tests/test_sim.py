"""Simulator fill model: path order inside bars, gaps, spread, break-even, partials and costs."""

import pytest

from gse.bars import Bar
from gse.broker.sim import SimBroker
from gse.config import CostSettings
from gse.models import Order, SymbolSpec


class Rec:
    def __init__(self):
        self.ev = []
        self.trades = []

    def on_order_filled(self, o, p, ts):
        self.ev.append(("fill", o.ref, p.entry))

    def on_order_cancelled(self, o, reason, ts):
        self.ev.append(("cancel", o.ref, reason))

    def on_order_rejected(self, o, reason, ts):
        self.ev.append(("reject", o.ref))

    def on_position_closed(self, p, t):
        self.ev.append(("close", t.ref, t.exit_reason, t.exit))
        self.trades.append(t)

    def on_position_event(self, p, kind, info, ts):
        self.ev.append((kind, p.ref))


def make(slip=0.0, comm=0.0, spread=0.2):
    sim = SimBroker(SymbolSpec(commission_per_unit_rt=comm), CostSettings(slippage_usd=slip, spread_usd=spread), 100_000)
    rec = Rec()
    sim.set_listener(rec)
    sim.process_bar(Bar(0, 2400, 2400.5, 2399.5, 2400, s=spread))
    return sim, rec


def order(ref, side, kind, entry, sl, tp, units=10, **kw):
    return Order(ref=ref, module="A", side=side, kind=kind, units=units, entry=entry, sl=sl, tp=tp,
                 risk_money=units * abs(entry - sl), r_dist=abs(entry - sl), created_ts=0, **kw)


def test_buy_stop_triggers_on_ask_and_oco_cancels_other_side():
    sim, rec = make()
    sim.place(order("L", 1, "stop", 2402.0, 2398.0, 2410.0, oco_group="g"))
    sim.place(order("S", -1, "stop", 2398.0, 2402.0, 2390.0, oco_group="g"))
    sim.process_bar(Bar(60, 2400, 2401.7, 2399.8, 2401.5, s=0.2))  # ask high 2401.9: not triggered
    assert not sim.positions
    sim.process_bar(Bar(120, 2401.5, 2402.0, 2401.0, 2401.9, s=0.2))  # ask reaches 2402.2
    assert ("cancel", "S", "OCO: other side filled") in rec.ev
    assert sim.positions["L"].entry == 2402.0


def test_same_bar_stop_and_target_follow_the_path():
    # up bar: open -> low -> high -> close, so the stop is hit before the target
    sim, rec = make(spread=0.0)
    sim.place(order("L", 1, "market", 2400, 2398.0, 2402.0))
    sim.process_bar(Bar(60, 2400, 2402.5, 2397.5, 2401.0, s=0.0))
    assert rec.ev[-1][:3] == ("close", "L", "sl")
    # down bar: open -> high -> low -> close, so the target is hit first
    sim, rec = make(spread=0.0)
    sim.place(order("L", 1, "market", 2400, 2398.0, 2402.0))
    sim.process_bar(Bar(60, 2400, 2402.5, 2397.5, 2399.0, s=0.0))
    assert rec.ev[-1][:3] == ("close", "L", "tp")


def test_gap_through_stop_fills_at_the_open():
    sim, rec = make(spread=0.0)
    sim.place(order("L", 1, "market", 2400, 2398.0, 2410.0))
    sim.process_bar(Bar(60, 2395.0, 2396.0, 2394.0, 2395.5, s=0.0))
    close = [e for e in rec.ev if e[0] == "close"][0]
    assert close[2] == "sl" and close[3] == 2395.0  # worse than the stop, never better


def test_short_stop_loss_uses_the_ask():
    sim, rec = make(spread=0.3)
    sim.place(order("S", -1, "market", 2400.0, 2402.0, 2390.0))
    assert sim.positions["S"].entry == 2400.0  # sells at the bid
    sim.process_bar(Bar(60, 2400.0, 2401.8, 2399.9, 2401.0, s=0.3))  # bid 2401.8 -> ask 2402.1
    assert rec.ev[-1][:3] == ("close", "S", "sl")


def test_breakeven_then_stopped_at_entry():
    sim, rec = make(spread=0.0)
    sim.place(order("L", 1, "market", 2400, 2396.0, 2410.0, be_trigger=2406.0))
    sim.process_bar(Bar(60, 2400, 2406.5, 2399.0, 2406.0, s=0.0))  # low first (no stop), then BE trigger
    assert sim.positions["L"].sl == 2400.0
    sim.process_bar(Bar(120, 2406, 2406.2, 2399.5, 2400.0, s=0.0))
    assert rec.ev[-1][:3] == ("close", "L", "breakeven")


def test_partial_target_and_accounting_with_costs():
    sim, rec = make(slip=0.05, comm=0.07, spread=0.2)
    start = sim.balance()
    o = order("B", -1, "market", 2400.0, 2404.0, 2392.0, units=20, partial_price=2396.0, partial_fraction=0.5,
              be_trigger=2396.0)
    sim.place(o)
    p = sim.positions["B"]
    assert p.entry == pytest.approx(2399.95)  # bid 2400 - slippage
    sim.process_bar(Bar(60, 2400.0, 2400.1, 2391.0, 2391.5, s=0.2))  # ask down to 2391.2: partial, BE, TP
    t = rec.trades[-1]
    assert t.exit_reason == "tp" and t.partial_taken and t.be_moved
    gross = (2399.95 - 2396.0) * 10 + (2399.95 - 2392.0) * 10
    comm = 20 * 0.07
    assert t.pnl == pytest.approx(gross - comm)
    assert sim.balance() - start == pytest.approx(t.pnl)
    assert t.exit == pytest.approx((2396.0 + 2392.0) / 2)


def test_market_buy_pays_spread_and_slippage():
    sim, rec = make(slip=0.05, spread=0.3)
    sim.place(order("L", 1, "market", 2400.3, 2396.0, 2410.0))
    assert sim.positions["L"].entry == pytest.approx(2400.35)
    sim.close("L", "manual")
    assert rec.trades[-1].exit == pytest.approx(2399.95)  # bid - slippage
