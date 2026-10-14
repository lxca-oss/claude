"""Shared data types for orders, positions and closed trades."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

LONG = 1
SHORT = -1


def side_name(side: int) -> str:
    return "BUY" if side > 0 else "SELL"


@dataclass
class SymbolSpec:
    """Contract details needed for sizing and P&L.

    Units are ounces. cTrader expresses volume in 1/100 of a unit, the broker adapter
    converts. ``quote_to_account`` converts USD P&L into the account currency.
    """

    name: str = "XAUUSD"
    digits: int = 2
    contract_size: float = 100.0
    min_units: float = 1.0
    step_units: float = 1.0
    max_units: float = 1_000_000.0
    commission_per_unit_rt: float = 0.07
    quote_to_account: float = 1.0

    def round_price(self, p: float) -> float:
        return round(p, self.digits)

    def floor_units(self, units: float) -> float:
        steps = math.floor(units / self.step_units + 1e-9)
        out = round(steps * self.step_units, 8)
        return min(out, self.max_units)

    def lots(self, units: float) -> float:
        return units / self.contract_size


@dataclass
class TradePlan:
    """A fully specified entry after the shared rules (stops, targets, sizing) were applied."""

    module: str
    side: int
    kind: str  # "market" | "stop"
    entry: float
    sl: float
    tp: float
    units: float
    risk_money: float
    r_dist: float
    be_trigger: float | None = None
    be_offset: float = 0.0
    partial_price: float | None = None
    partial_fraction: float = 0.0
    oco_group: str | None = None
    expiry_ts: int | None = None
    tag: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def target_r(self) -> float:
        return abs(self.tp - self.entry) / self.r_dist if self.r_dist else 0.0


@dataclass
class Order:
    """A pending (stop) or market order request tracked by the engine and broker."""

    ref: str
    module: str
    side: int
    kind: str
    units: float
    entry: float
    sl: float
    tp: float
    risk_money: float
    r_dist: float
    created_ts: int
    be_trigger: float | None = None
    be_offset: float = 0.0
    partial_price: float | None = None
    partial_fraction: float = 0.0
    oco_group: str | None = None
    expiry_ts: int | None = None
    tag: str = ""
    notes: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | filled | cancelled | rejected
    broker_id: int | None = None

    @classmethod
    def from_plan(cls, ref: str, plan: TradePlan, ts: int) -> "Order":
        return cls(
            ref=ref, module=plan.module, side=plan.side, kind=plan.kind, units=plan.units,
            entry=plan.entry, sl=plan.sl, tp=plan.tp, risk_money=plan.risk_money, r_dist=plan.r_dist,
            created_ts=ts, be_trigger=plan.be_trigger, be_offset=plan.be_offset,
            partial_price=plan.partial_price, partial_fraction=plan.partial_fraction,
            oco_group=plan.oco_group, expiry_ts=plan.expiry_ts, tag=plan.tag, notes=list(plan.notes),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Position:
    ref: str
    module: str
    side: int
    units: float
    initial_units: float
    entry: float
    sl: float
    tp: float
    open_ts: int
    risk_money: float
    r_dist: float
    initial_sl: float
    planned_entry: float
    be_trigger: float | None = None
    be_offset: float = 0.0
    be_done: bool = False
    partial_price: float | None = None
    partial_fraction: float = 0.0
    partial_done: bool = False
    realized_pnl: float = 0.0  # from partial closes, net of their commission
    commission: float = 0.0  # all commission charged so far (account ccy, positive number)
    exit_value: float = 0.0  # sum(units * price) of partial exits, for the average exit price
    exited_units: float = 0.0
    best: float = 0.0  # most favourable price seen
    worst: float = 0.0  # most adverse price seen
    tag: str = ""
    notes: list[str] = field(default_factory=list)
    broker_id: int | None = None

    def __post_init__(self):
        if not self.best:
            self.best = self.entry
        if not self.worst:
            self.worst = self.entry

    def mark(self, bid: float, ask: float) -> float:
        """Price at which the position would close now."""
        return bid if self.side > 0 else ask

    def unrealized_quote(self, bid: float, ask: float) -> float:
        return (self.mark(bid, ask) - self.entry) * self.side * self.units

    def r_now(self, bid: float, ask: float) -> float:
        if not self.r_dist:
            return 0.0
        return (self.mark(bid, ask) - self.entry) * self.side / self.r_dist

    def to_dict(self, bid: float | None = None, ask: float | None = None) -> dict:
        d = asdict(self)
        if bid is not None and ask is not None:
            d["r_now"] = self.r_now(bid, ask)
            d["mark"] = self.mark(bid, ask)
        return d


@dataclass
class ClosedTrade:
    ref: str
    module: str
    side: int
    units: float
    entry: float
    exit: float
    planned_entry: float
    sl_initial: float
    tp: float
    open_ts: int
    close_ts: int
    pnl: float  # net, account currency
    commission: float
    risk_money: float
    r: float
    exit_reason: str
    mfe_r: float
    mae_r: float
    partial_taken: bool
    be_moved: bool
    r_dist: float = 0.0
    be_trigger: float | None = None
    partial_price: float | None = None
    partial_fraction: float = 0.0
    tag: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side_name"] = side_name(self.side)
        return d
