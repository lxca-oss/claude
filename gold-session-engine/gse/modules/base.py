"""Base class for strategy modules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..bars import Bar
from ..models import ClosedTrade, Order, Position
from ..risk import classify

if TYPE_CHECKING:
    from ..engine import StrategyEngine


class Module:
    name = "?"
    title = ""
    window = ""

    def __init__(self, engine: "StrategyEngine"):
        self.e = engine
        self.reset_day()

    @property
    def cfg(self):
        return self.e.s.module(self.name)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def reset_day(self) -> None:
        self.trades = 0
        self.losses = 0
        self.refs: set[str] = set()
        self.status = "waiting for its window"
        self.detail: dict = {}

    # hooks -----------------------------------------------------------------------
    def on_m1(self, bar: Bar) -> None:
        pass

    def on_m5(self, bar: Bar) -> None:
        pass

    def on_fill(self, order: Order, position: Position) -> None:
        self.refs.discard(order.ref)
        self.trades += 1

    def on_cancel(self, order: Order, reason: str) -> None:
        self.refs.discard(order.ref)

    def on_close(self, trade: ClosedTrade) -> None:
        if classify(trade.r, self.e.s.risk.scratch_r) == "loss":
            self.losses += 1
        self.status = f"last trade closed ({trade.exit_reason}) {trade.r:+.2f}R"

    def restore_trade(self, t: dict) -> None:
        """Re-count a trade taken earlier today (bot restarted mid-session)."""
        self.trades += 1
        if t.get("status") == "closed" and t.get("r") is not None:
            if classify(t["r"], self.e.s.risk.scratch_r) == "loss":
                self.losses += 1
        self.status = f"restored: {self.trades} trade(s) already taken today"

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "window": self.window,
            "enabled": self.enabled,
            "trades": self.trades,
            "losses": self.losses,
            "status": self.status,
            "detail": self.detail,
            "pending": sorted(self.refs),
        }
