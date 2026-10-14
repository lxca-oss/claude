"""Bot settings.

Every rule from the strategy document is a parameter here so that modules can be
switched on and off, and variants can be tested without touching code. Defaults follow
the document; RULES.md explains how each ambiguous rule was pinned down.
"""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import paths


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _check_hhmm(v: str) -> str:
    parse_hhmm(v)
    return v


class RiskSettings(_Model):
    risk_per_trade_pct: float = Field(0.3, gt=0, le=5, description="Risk per trade, % of balance")
    risk_basis: Literal["balance", "initial"] = Field(
        "balance", description="Size from current balance or from the starting balance"
    )
    max_trades_per_day: int = Field(8, ge=1, le=50, description="Ceiling, not a quota")
    daily_loss_stop_pct: float = Field(1.5, gt=0, description="Stop for the day at this equity loss, %")
    max_consecutive_losses: int = Field(3, ge=1, description="Stop for the day after this many losses in a row")
    daily_profit_stop_pct: float = Field(2.0, gt=0, description="Stop for the day at this equity gain, % (Best Day Rule)")
    flatten_on_daily_stop: bool = Field(True, description="Close open positions when the daily loss/profit stop hits")
    min_stop_usd: float = Field(3.0, ge=0, description="Minimum stop distance, $")
    min_stop_atr_mult: float = Field(1.0, ge=0, description="Minimum stop distance as a multiple of M5 ATR(14)")
    sizing_slippage_usd: float = Field(
        0.10, ge=0, description="Extra $/oz assumed lost at the stop when sizing (slippage allowance)"
    )
    scratch_r: float = Field(
        0.05, ge=0, description="Trades within ±this many R count as scratches, not wins or losses"
    )


class LevelSettings(_Model):
    step: float = Field(20.0, gt=0, description="Round-level spacing, $")
    target_offset: float = Field(1.0, ge=0, description="Put targets this far short of the next level, $")
    min_room_r: float = Field(1.2, ge=0, description="Skip the trade if less than this many R of room")
    stop_near_zone: float = Field(2.0, ge=0, description="Stops may not sit within this distance on the near side, $")
    stop_beyond: float = Field(2.5, ge=0, description="Push stops at least this far beyond the level, $")


class SessionSettings(_Model):
    asian_start: str = Field("00:00", description="Asian range start, London time")
    asian_end: str = Field("07:00", description="Asian range end, London time")
    london_start: str = Field("07:00", description="Module A/B window start, London time")
    london_end: str = Field("10:00", description="Module A/B window end, London time")
    ny_start: str = Field("08:20", description="Module C window start, New York time")
    ny_end: str = Field("11:00", description="Module C window end, New York time")
    pmfix_start: str = Field("09:45", description="Module D window start, New York time")
    pmfix_end: str = Field("10:30", description="Module D window end, New York time")
    move_anchor_ny: str = Field("08:30", description="Module D measures the move from here, New York time")
    no_new_trades_ny: str = Field("11:00", description="No new trades after this, New York time")
    flat_ny: str = Field("11:30", description="Flat by this time, New York time")
    server_close_ny: str = Field(
        "14:00",
        description="21:00 on a New-York-close broker server (NY+7h), used by Module A's alternative exit",
    )
    min_asian_coverage: float = Field(
        0.8, ge=0, le=1, description="Fraction of Asian-range minutes that must have data"
    )

    _v = field_validator(
        "asian_start", "asian_end", "london_start", "london_end", "ny_start", "ny_end",
        "pmfix_start", "pmfix_end", "move_anchor_ny", "no_new_trades_ny", "flat_ny", "server_close_ny",
    )(_check_hhmm)


class NewsSettings(_Model):
    enabled: bool = True
    before_minutes: int = Field(5, ge=0)
    after_minutes: int = Field(5, ge=0)
    major_after_minutes: int = Field(15, ge=0, description="After NFP, CPI and FOMC")
    currencies: list[str] = ["USD"]
    impacts: list[str] = ["High"]
    major_keywords: list[str] = [
        "Non-Farm", "NFP", "CPI", "FOMC", "Federal Funds Rate", "Fed Interest Rate",
    ]
    feed_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    refresh_minutes: int = Field(60, ge=5)


class ModuleASettings(_Model):
    enabled: bool = True
    range_min_pct: float = Field(0.30, ge=0, description="Skip the day if the Asian range is narrower, % of price")
    range_max_pct: float = Field(1.60, ge=0, description="Skip the day if the Asian range is wider, % of price")
    entry_buffer: float = Field(0.50, ge=0, description="Stop orders sit this far beyond the range, $")
    stop_cap_pct: float = Field(0.6, gt=0, description="Stop distance cap, % of price")
    target_r: float = Field(2.5, gt=0)
    breakeven_r: float = Field(1.5, ge=0, description="Move stop to break-even at this R (0 = off)")
    order_expiry: Literal["window_end", "no_new_trades"] = Field(
        "window_end", description="Cancel unfilled orders at 10:00 London or at 11:00 New York"
    )
    exit_mode: Literal["session_close", "server_2100"] = Field(
        "session_close", description="Flat at 11:30 NY (shared rule) or at 21:00 server time (source)"
    )
    apply_level_rules: bool = Field(True, description="Apply the shared $20-level stop/target rules")
    max_trades: int = Field(1, ge=0)


class ModuleBSettings(_Model):
    enabled: bool = True
    sweep_min: float = Field(1.0, ge=0, description="Price must trade this far beyond the range, $")
    reclaim_bars: int = Field(3, ge=0, description="M5 bars after the sweep bar allowed for the reclaim close")
    stop_buffer_min: float = Field(1.5, ge=0, description="Stop beyond the wick by at least this, $")
    stop_buffer_atr: float = Field(0.5, ge=0, description="...or this multiple of M5 ATR, whichever is larger")
    partial_fraction: float = Field(0.5, gt=0, lt=1, description="Fraction closed at the range midpoint")
    final_target_offset: float = Field(1.0, ge=0, description="Final target sits this far inside the opposite edge, $")
    max_per_side: int = Field(1, ge=0)
    apply_level_rules: bool = True


class ModuleCSettings(_Model):
    enabled: bool = True
    ema_period: int = Field(20, ge=2)
    slope_bars: int = Field(3, ge=1, description="M15 EMA slope measured over this many M15 bars")
    touch_tolerance: float = Field(0.20, ge=0, description="A pullback 'touches' the EMA/VWAP within this distance, $")
    setup_expiry_bars: int = Field(6, ge=1, description="A setup stays armed for this many M5 bars after the last touch")
    swing_strength: int = Field(2, ge=1, description="M1 bars each side that define a swing high/low")
    swing_fallback_bars: int = Field(5, ge=1, description="If no M1 swing yet, use the extreme of this many M1 bars")
    entry_buffer: float = Field(0.10, ge=0, description="Trigger this far beyond the M1 swing, $")
    stop_buffer: float = Field(0.30, ge=0, description="Stop this far beyond the pullback extreme, $")
    target_r: float = Field(2.0, gt=0, description="1.5–2R per the document")
    max_trades: int = Field(3, ge=0)
    max_losses: int = Field(2, ge=1)
    apply_level_rules: bool = True


class ModuleDSettings(_Model):
    enabled: bool = Field(False, description="Experimental: stays off until it passes validation")
    move_atr_mult: float = Field(1.5, gt=0, description="Move since 08:30 NY must be ≥ this × M15 ATR(14)")
    level_proximity: float = Field(2.0, ge=0, description="Price within this distance of a $20 level, $")
    stop_beyond_level: float = Field(2.5, ge=0)
    extreme_buffer: float = Field(0.5, ge=0, description="Stop at least this far beyond the move's extreme, $")
    target_r: float = Field(1.5, gt=0)
    max_trades: int = Field(2, ge=0)
    apply_level_rules: bool = True


class CostSettings(_Model):
    """Trading costs used by the simulator (backtest, replay and paper modes)."""

    spread_usd: float = Field(0.25, ge=0, description="Used when the data has no spread column, $")
    commission_per_oz_rt: float = Field(0.07, ge=0, description="Round-turn commission per ounce, $")
    slippage_usd: float = Field(0.05, ge=0, description="Adverse slippage on stop and market fills, $")
    contract_size_oz: float = Field(100.0, gt=0, description="Ounces per lot")
    min_lots: float = Field(0.01, gt=0)
    lot_step: float = Field(0.01, gt=0)


class BacktestSettings(_Model):
    initial_balance: float = Field(100_000.0, gt=0)
    split_date: str = Field("2022-01-01", description="First day of the second half for the two-halves test")
    random_iterations: int = Field(200, ge=0, le=5000)
    random_seed: int = 42
    ftmo_daily_loss_pct: float = 5.0
    ftmo_max_loss_pct: float = 10.0
    best_day_max_share: float = 0.30
    target_t_stat: float = 3.0
    module_a_target_pf: float = 1.3


class CTraderSettings(_Model):
    environment: Literal["demo", "live"] = "demo"
    account_id: int | None = Field(None, description="ctidTraderAccountId of the chosen account")
    symbol: str = "XAUUSD"
    label: str = Field("GSE", description="Label stamped on every order so the bot only manages its own trades")
    redirect_uri: str = "http://localhost:8050/oauth/callback"
    commission_per_oz_rt: float | None = Field(
        None, description="Override the commission used for sizing; empty = derive from the symbol"
    )


class TerminalSettings(_Model):
    host: str = "127.0.0.1"
    port: int = 8050


class Settings(_Model):
    mode: Literal["paper", "live"] = Field(
        "paper", description="paper = live cTrader prices, simulated fills; live = real orders on the account"
    )
    autostart: bool = Field(False, description="Start the bot automatically when the terminal starts")
    risk: RiskSettings = RiskSettings()
    levels: LevelSettings = LevelSettings()
    sessions: SessionSettings = SessionSettings()
    news: NewsSettings = NewsSettings()
    module_a: ModuleASettings = ModuleASettings()
    module_b: ModuleBSettings = ModuleBSettings()
    module_c: ModuleCSettings = ModuleCSettings()
    module_d: ModuleDSettings = ModuleDSettings()
    costs: CostSettings = CostSettings()
    backtest: BacktestSettings = BacktestSettings()
    ctrader: CTraderSettings = CTraderSettings()
    terminal: TerminalSettings = TerminalSettings()

    def module(self, name: str):
        return getattr(self, f"module_{name.lower()}")

    def copy_with(self, overrides: dict) -> "Settings":
        """Return a copy with dotted-path overrides applied, e.g. {"module_a.target_r": 2.0}."""
        data = self.model_dump()
        for key, value in overrides.items():
            node = data
            parts = key.split(".")
            for p in parts[:-1]:
                node = node[p]
            if parts[-1] not in node:
                raise KeyError(f"Unknown setting: {key}")
            node[parts[-1]] = value
        return Settings.model_validate(data)


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(path: Path | None = None) -> Settings:
    path = path or paths.config_path()
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        return Settings.model_validate(raw)
    return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(settings.model_dump(), sort_keys=False))


# --- secrets (cTrader app credentials and tokens) ---------------------------------

SECRET_KEYS = ("client_id", "client_secret", "access_token", "refresh_token", "token_expires_at")


def load_secrets() -> dict:
    p = paths.secrets_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save_secrets(secrets: dict) -> None:
    p = paths.secrets_path()
    p.write_text(json.dumps({k: v for k, v in secrets.items() if v is not None}, indent=2))
    try:
        p.chmod(0o600)
    except OSError:  # Windows
        pass
