"""Broker interface shared by the simulator and the cTrader adapter.

The engine is synchronous: it calls these methods and gets results back through the
listener callbacks. The simulator calls back immediately; the cTrader broker sends the
request and calls back when the server's execution event arrives. Orders and positions
are identified by the engine's own ``ref`` string, which is sent to cTrader as the
``clientOrderId`` and position comment so the mapping survives restarts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from ..models import ClosedTrade, Order, Position, SymbolSpec


class BrokerListener(Protocol):
    def on_order_filled(self, order: Order, position: Position, ts: int) -> None: ...

    def on_order_cancelled(self, order: Order, reason: str, ts: int) -> None: ...

    def on_order_rejected(self, order: Order, reason: str, ts: int) -> None: ...

    def on_position_closed(self, position: Position, trade: ClosedTrade) -> None: ...

    def on_position_event(self, position: Position, kind: str, info: dict, ts: int) -> None: ...


class Broker(ABC):
    listener: BrokerListener | None = None
    symbol: SymbolSpec

    def set_listener(self, listener: BrokerListener) -> None:
        self.listener = listener

    @abstractmethod
    def balance(self) -> float: ...

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def quote(self) -> tuple[float, float]:
        """(bid, ask)"""

    @abstractmethod
    def place(self, order: Order) -> None: ...

    @abstractmethod
    def amend(self, ref: str, entry: float, sl: float, tp: float, units: float,
              be_trigger: float | None = None) -> None: ...

    @abstractmethod
    def cancel(self, ref: str, reason: str) -> None: ...

    @abstractmethod
    def modify_position(self, ref: str, sl: float | None = None, tp: float | None = None) -> None: ...

    @abstractmethod
    def close(self, ref: str, reason: str, units: float | None = None) -> None: ...

    @abstractmethod
    def open_positions(self) -> list[Position]: ...

    @abstractmethod
    def pending_orders(self) -> list[Order]: ...


def make_closed_trade(p: Position, close_ts: int, exit_price: float, units: float, reason: str,
                      final_pnl: float, final_commission: float) -> ClosedTrade:
    """Build the closed-trade record from a position's partial exits plus the final exit.

    ``final_pnl`` is net of ``final_commission``; both are in account currency.
    """
    exit_value = p.exit_value + exit_price * units
    exited = p.exited_units + units
    pnl = p.realized_pnl + final_pnl
    commission = p.commission + final_commission
    avg_exit = exit_value / exited if exited else exit_price
    r = pnl / p.risk_money if p.risk_money else 0.0
    mfe_r = (p.best - p.entry) * p.side / p.r_dist if p.r_dist else 0.0
    mae_r = (p.worst - p.entry) * p.side / p.r_dist if p.r_dist else 0.0
    return ClosedTrade(
        ref=p.ref, module=p.module, side=p.side, units=p.initial_units, entry=p.entry, exit=avg_exit,
        planned_entry=p.planned_entry, sl_initial=p.initial_sl, tp=p.tp, open_ts=p.open_ts,
        close_ts=close_ts, pnl=pnl, commission=commission, risk_money=p.risk_money, r=r,
        exit_reason=reason, mfe_r=mfe_r, mae_r=mae_r, partial_taken=p.partial_done, be_moved=p.be_done,
        r_dist=p.r_dist, be_trigger=p.be_trigger, partial_price=p.partial_price,
        partial_fraction=p.partial_fraction, tag=p.tag, notes=list(p.notes),
    )
