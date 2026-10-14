"""Shared rules: stop/target placement, position sizing and the daily risk manager.

The order in which the shared rules are applied to a module's raw signal:

1. widen the stop to the minimum distance, max($3, 1 x M5 ATR(14));
2. push the stop away from a nearby $20 level (never within $2 on the near side, at
   least $2.5 beyond);
3. measure R from the final stop and set the target (R multiple and/or a price);
4. pull the target in to $1 short of the next $20 level if that level comes first;
5. skip if fewer than 1.2R of room remain;
6. size from the money actually lost at the stop: units x (stop distance + slippage
   allowance + round-turn commission), converted to the account currency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import LevelSettings, RiskSettings
from .levels import adjust_stop, adjust_target
from .models import SymbolSpec, TradePlan


@dataclass
class Signal:
    module: str
    side: int
    kind: str  # "market" | "stop"
    entry: float
    stop: float
    target_r: float | None = None
    target_price: float | None = None
    target_mode: str = "r"  # "r" | "price" | "nearer"
    be_at_r: float | None = None
    be_after_partial: bool = False
    partial_price: float | None = None
    partial_fraction: float = 0.0
    apply_level_rules: bool = True
    oco_group: str | None = None
    expiry_ts: int | None = None
    tag: str = ""
    notes: list[str] = field(default_factory=list)


class Skip(str):
    """A reason a signal did not become a trade."""


class TradePlanner:
    def __init__(self, risk: RiskSettings, levels: LevelSettings, symbol: SymbolSpec):
        self.risk = risk
        self.levels = levels
        self.symbol = symbol

    def min_stop(self, atr_m5: float | None) -> float:
        return max(self.risk.min_stop_usd, self.risk.min_stop_atr_mult * (atr_m5 or 0.0))

    def cost_per_unit(self, dist: float) -> float:
        """Account-currency money lost per unit if the stop is hit."""
        sym = self.symbol
        return (dist + self.risk.sizing_slippage_usd + sym.commission_per_unit_rt) * sym.quote_to_account

    def size(self, risk_money: float, dist: float) -> float:
        per_unit = self.cost_per_unit(dist)
        if per_unit <= 0:
            return 0.0
        return self.symbol.floor_units(risk_money / per_unit)

    def plan(self, sig: Signal, atr_m5: float | None, sizing_balance: float) -> TradePlan | Skip:
        side, entry, stop = sig.side, sig.entry, sig.stop
        notes = list(sig.notes)
        if (entry - stop) * side <= 0:
            return Skip("stop on the wrong side of entry")

        min_dist = self.min_stop(atr_m5)
        if abs(entry - stop) < min_dist:
            stop = entry - side * min_dist
            notes.append(f"stop widened to min distance {min_dist:.2f}")

        if sig.apply_level_rules:
            stop, moved = adjust_stop(side, entry, stop, self.levels)
            if moved:
                notes.append(f"stop pushed beyond $20 level to {stop:.2f}")

        stop = self.symbol.round_price(stop)
        dist = abs(entry - stop)

        r_target = entry + side * sig.target_r * dist if sig.target_r else None
        if sig.target_mode == "r":
            tp = r_target
        elif sig.target_mode == "price":
            tp = sig.target_price
        else:  # nearer of the two (ignoring a price target on the wrong side)
            cands = [x for x in (r_target, sig.target_price) if x is not None and (x - entry) * side > 0]
            tp = min(cands, key=lambda x: abs(x - entry)) if cands else None
        if tp is None or (tp - entry) * side <= 0:
            return Skip("no target in the trade direction")

        if sig.apply_level_rules:
            tp, moved = adjust_target(side, entry, tp, self.levels)
            if moved:
                notes.append(f"target pulled to $1 short of $20 level: {tp:.2f}")
        tp = self.symbol.round_price(tp)

        room = (tp - entry) * side / dist
        if room < self.levels.min_room_r - 1e-9:
            return Skip(f"room {room:.2f}R < {self.levels.min_room_r}R")

        partial_price, partial_fraction = None, 0.0
        if sig.partial_price is not None:
            if 0 < (sig.partial_price - entry) * side < (tp - entry) * side:
                partial_price = self.symbol.round_price(sig.partial_price)
                partial_fraction = sig.partial_fraction
            else:
                notes.append("partial target not between entry and final target: dropped")

        be_trigger = None
        if sig.be_after_partial and partial_price is not None:
            be_trigger = partial_price
        elif sig.be_at_r:
            be_trigger = self.symbol.round_price(entry + side * sig.be_at_r * dist)

        risk_money = sizing_balance * self.risk.risk_per_trade_pct / 100.0
        units = self.size(risk_money, dist)
        if units < self.symbol.min_units:
            return Skip(f"position size {units:g} below broker minimum {self.symbol.min_units:g}")
        if partial_price is not None and self.symbol.floor_units(units * partial_fraction) < self.symbol.min_units:
            notes.append("position too small to split: partial dropped")
            partial_price, partial_fraction = None, 0.0
            if sig.be_after_partial:
                be_trigger = None

        return TradePlan(
            module=sig.module, side=side, kind=sig.kind, entry=self.symbol.round_price(entry), sl=stop, tp=tp,
            units=units, risk_money=units * self.cost_per_unit(dist), r_dist=dist, be_trigger=be_trigger,
            partial_price=partial_price, partial_fraction=partial_fraction, oco_group=sig.oco_group,
            expiry_ts=sig.expiry_ts, tag=sig.tag, notes=notes,
        )


@dataclass
class DailyRisk:
    """Per-day counters for the shared daily limits."""

    day: date
    start_balance: float
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    realized_pnl: float = 0.0
    stopped: str | None = None  # reason trading is stopped for the day
    module_trades: dict = field(default_factory=dict)
    module_losses: dict = field(default_factory=dict)
    min_equity: float | None = None
    max_equity: float | None = None

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "start_balance": self.start_balance,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "realized_pnl": self.realized_pnl,
            "stopped": self.stopped,
            "module_trades": dict(self.module_trades),
            "module_losses": dict(self.module_losses),
        }


def classify(r: float, scratch: float) -> str:
    if r > scratch:
        return "win"
    if r < -scratch:
        return "loss"
    return "scratch"
