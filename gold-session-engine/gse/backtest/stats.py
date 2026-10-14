"""Performance statistics and the validation gates from the strategy document."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from ..config import Settings

MODULES = ("A", "B", "C", "D")


def trade_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    pnl = [t["pnl"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p < 0)
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    # drawdown on the cumulative R curve
    peak = cum = max_dd = 0.0
    streak = max_streak = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        streak = streak + 1 if r < 0 else 0
        max_streak = max(max_streak, streak)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    breakeven_wr = (-avg_loss / (avg_win - avg_loss)) if wins and losses and avg_win - avg_loss else None
    durations = [(t["close_ts"] - t["open_ts"]) / 60 for t in trades]
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "mean_r": mean,
        "median_r": sorted(rs)[n // 2],
        "sd_r": sd,
        "t_stat": t_stat,
        "total_r": sum(rs),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "net_pnl": sum(pnl),
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "breakeven_win_rate": breakeven_wr,
        "max_dd_r": max_dd,
        "max_losing_streak": max_streak,
        "avg_minutes": sum(durations) / n,
        "commission": sum(t.get("commission", 0.0) for t in trades),
    }


def by_module(trades: list[dict]) -> dict:
    out = {}
    for m in MODULES:
        sub = [t for t in trades if t["module"] == m]
        if sub:
            out[m] = trade_stats(sub)
    return out


def split_halves(trades: list[dict], split_ts: int) -> dict:
    first = [t for t in trades if t["open_ts"] < split_ts]
    second = [t for t in trades if t["open_ts"] >= split_ts]
    return {
        "first": {"all": trade_stats(first), **by_module(first)},
        "second": {"all": trade_stats(second), **by_module(second)},
    }


def equity_stats(days: list[dict], initial: float, settings: Settings) -> dict:
    """Account-level numbers in the FTMO sense: worst day vs its start, max drawdown vs initial."""
    if not days:
        return {}
    peak = initial
    max_dd_pct = 0.0
    max_dd_from_initial_pct = 0.0
    worst_day_pct = 0.0
    best_day = 0.0
    daily_pnls = []
    for d in days:
        start = d["open_balance"]
        worst_day_pct = min(worst_day_pct, (d["min_equity"] - start) / start * 100)
        peak = max(peak, d["max_equity"])
        max_dd_pct = max(max_dd_pct, (peak - d["min_equity"]) / peak * 100)
        max_dd_from_initial_pct = max(max_dd_from_initial_pct, (initial - d["min_equity"]) / initial * 100)
        if d["pnl"] != 0:
            daily_pnls.append(d["pnl"])
        best_day = max(best_day, d["pnl"])
    final = days[-1]["close_balance"]
    total_profit = final - initial
    b = settings.backtest
    return {
        "final_balance": final,
        "return_pct": total_profit / initial * 100,
        "max_drawdown_pct": max_dd_pct,
        "max_loss_from_initial_pct": max_dd_from_initial_pct,
        "worst_day_pct": worst_day_pct,
        "best_day": best_day,
        "best_day_share": (best_day / total_profit) if total_profit > 0 else None,
        "trading_days": len(daily_pnls),
        "ftmo_daily_loss_limit_pct": b.ftmo_daily_loss_pct,
        "ftmo_max_loss_limit_pct": b.ftmo_max_loss_pct,
        "ftmo_daily_breached": -worst_day_pct >= b.ftmo_daily_loss_pct,
        "ftmo_max_breached": max_dd_from_initial_pct >= b.ftmo_max_loss_pct,
    }


def monthly_returns(days: list[dict]) -> list[dict]:
    months: dict[str, dict] = {}
    for d in days:
        key = d["day"][:7]
        m = months.setdefault(key, {"month": key, "open": d["open_balance"], "close": d["close_balance"]})
        m["close"] = d["close_balance"]
    out = []
    for m in months.values():
        m["pnl"] = m["close"] - m["open"]
        m["return_pct"] = m["pnl"] / m["open"] * 100 if m["open"] else 0.0
        out.append(m)
    return out


def yearly(trades: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for t in trades:
        groups[datetime.fromtimestamp(t["open_ts"], timezone.utc).year].append(t)
    return [{"year": y, **trade_stats(ts)} for y, ts in sorted(groups.items())]


def trades_per_day(trades: list[dict], days: list[dict]) -> dict:
    per = defaultdict(int)
    for t in trades:
        per[datetime.fromtimestamp(t["open_ts"], timezone.utc).date().isoformat()] += 1
    weekdays = [d for d in days if datetime.fromisoformat(d["day"]).weekday() < 5]
    counts = [per.get(d["day"], 0) for d in weekdays]
    hist = defaultdict(int)
    for c in counts:
        hist[c] += 1
    return {
        "average": sum(counts) / len(counts) if counts else 0.0,
        "max": max(counts) if counts else 0,
        "histogram": {str(k): v for k, v in sorted(hist.items())},
    }


def gates(settings: Settings, modules: dict, halves: dict, baseline: dict, equity: dict) -> list[dict]:
    """Evaluate the validation plan's pass criteria."""
    b = settings.backtest
    out = []

    def add(gate, module, value, threshold, ok, note=""):
        out.append({"gate": gate, "module": module, "value": value, "threshold": threshold,
                    "pass": ok, "note": note})

    a = modules.get("A")
    if a and settings.module_a.enabled:
        pf = a.get("profit_factor", 0.0)
        add("Module A reproduction", "A", pf, f"PF ≥ {b.module_a_target_pf}", pf >= b.module_a_target_pf,
            "if this fails, debug the build before testing anything else")
    for m, st in modules.items():
        bl = baseline.get(m)
        if bl and bl.get("iterations"):
            add("Beats random entries", m, bl["percentile"], "≥ 95th percentile of random means",
                bl["percentile"] >= 95,
                f"module mean {st['mean_r']:+.3f}R vs random {bl['random_mean']:+.3f}R")
        h1 = halves["first"].get(m, {}).get("mean_r")
        h2 = halves["second"].get(m, {}).get("mean_r")
        if h1 is not None and h2 is not None:
            add("Both halves positive", m, [h1, h2], "mean R > 0 in each", h1 > 0 and h2 > 0)
        else:
            add("Both halves positive", m, [h1, h2], "mean R > 0 in each", False,
                "no trades in one half: the data does not cover both periods")
        add("Significance", m, st.get("t_stat", 0.0), f"t ≥ {b.target_t_stat}", st.get("t_stat", 0.0) >= b.target_t_stat)
    if equity:
        worst = -equity["worst_day_pct"]
        dd = equity["max_loss_from_initial_pct"]
        ok = worst <= b.ftmo_daily_loss_pct * 0.5 and dd <= b.ftmo_max_loss_pct * 0.5
        add("Combined drawdown", "all", {"worst_day_pct": worst, "max_loss_pct": dd},
            f"≤ half of FTMO limits ({b.ftmo_daily_loss_pct}% daily, {b.ftmo_max_loss_pct}% max)", ok)
        if equity.get("best_day_share") is not None:
            share = equity["best_day_share"]
            add("Best Day Rule", "all", share, f"best day ≤ {b.best_day_max_share:.0%} of total profit",
                share <= b.best_day_max_share)
    add("Forward test", "all", None, "1–2 months demo + FTMO free trial within the backtest's range", None,
        "manual: compare the live journal with this backtest")
    return out
