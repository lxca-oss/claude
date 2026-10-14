"""Full backtest pipeline: data -> engine run -> stats -> random baseline -> gates."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

from ..config import Settings
from ..news import NewsCalendar
from . import data as datastore
from .baseline import random_baseline
from .runner import run_backtest
from .stats import by_module, equity_stats, gates, monthly_returns, split_halves, trade_stats, trades_per_day, yearly

PRESETS = {
    "doc_defaults": {
        "label": "Document defaults (A+B+C, shared rules on)",
        "overrides": {},
    },
    "module_a_reproduction": {
        "label": "Module A reproduction (published rules: A only, no $20-level rules, 1% risk)",
        "overrides": {
            "module_b.enabled": False, "module_c.enabled": False, "module_d.enabled": False,
            "module_a.apply_level_rules": False, "risk.risk_per_trade_pct": 1.0,
            "risk.daily_loss_stop_pct": 100.0, "risk.daily_profit_stop_pct": 100.0,
            "risk.max_consecutive_losses": 99,
        },
    },
    "module_a_alone": {"label": "Module A alone (shared rules on)", "overrides": {
        "module_b.enabled": False, "module_c.enabled": False, "module_d.enabled": False}},
    "module_b_alone": {"label": "Module B alone", "overrides": {
        "module_a.enabled": False, "module_c.enabled": False, "module_d.enabled": False}},
    "module_c_alone": {"label": "Module C alone", "overrides": {
        "module_a.enabled": False, "module_b.enabled": False, "module_d.enabled": False}},
    "module_d_alone": {"label": "Module D alone (experimental)", "overrides": {
        "module_a.enabled": False, "module_b.enabled": False, "module_c.enabled": False,
        "module_d.enabled": True}},
}


def clean(x):
    """Make a result JSON-safe (no inf/nan)."""
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def build_report(settings: Settings, raw: dict, baseline: dict, news_coverage: float | None, meta: dict) -> dict:
    trades = raw["trades"]
    fill_model = raw.get("fill_model", "bars")
    split = datetime.fromisoformat(settings.backtest.split_date).replace(tzinfo=timezone.utc)
    modules = by_module(trades)
    halves = split_halves(trades, int(split.timestamp()))
    equity = equity_stats(raw["days"], raw["initial_balance"], settings)
    warnings = []
    if meta.get("synthetic"):
        warnings.append("SYNTHETIC DATA: results only show the software works. They say nothing about the strategy.")
    if meta.get("spread_coverage", 0) < 0.5:
        warnings.append(
            f"The data has no real spread for most bars; a fixed ${settings.costs.spread_usd:.2f} spread was assumed."
        )
    if fill_model == "bars" and (settings.module_b.enabled or settings.module_c.enabled):
        warnings.append(
            "Fills use M1 bars with an assumed path inside each minute. Modules B and C (small stops, M1 "
            "triggers) are sensitive to that assumption: confirm them with a tick-replay backtest on cTrader ticks."
        )
    if news_coverage is None and settings.news.enabled:
        warnings.append("The news filter was switched off for this run.")
    if news_coverage is not None and news_coverage < 0.5 and settings.news.enabled:
        warnings.append(
            f"News calendar covers only {news_coverage:.0%} of the tested weeks: the news filter was mostly "
            "inactive. Import historical high-impact USD events (Data tab) for a faithful test."
        )
    if settings.module_a.enabled and settings.module_a.apply_level_rules:
        a_room = sum(v for k, v in raw["skips"].items() if k.startswith("A:") and "room" in k)
        if a_room:
            warnings.append(
                f"Module A skipped {a_room} days for lack of room after the $20-level target rule. With $20 "
                "spacing any target more than $20 away is always cut back, so the shared rule conflicts with "
                "Module A's 2.5R target. Use the 'Module A reproduction' preset for validation gate 2."
            )
    if raw["start_ts"] and raw["end_ts"]:
        if raw["start_ts"] > split.timestamp() or raw["end_ts"] < split.timestamp():
            warnings.append(
                f"The data does not span the split date {settings.backtest.split_date}, so the two-halves test "
                "cannot pass. Use data from 2018 to 2026."
            )
    report = {
        "fill_model": fill_model,
        "dataset": meta,
        "start": _d(raw["start_ts"]),
        "end": _d(raw["end_ts"]),
        "bars": raw["bars"],
        "elapsed_s": raw["elapsed_s"],
        "summary": {**trade_stats(trades), **equity},
        "modules": modules,
        "halves": halves,
        "yearly": yearly(trades),
        "monthly": monthly_returns(raw["days"]),
        "trades_per_day": trades_per_day(trades, raw["days"]),
        "equity": [
            {"day": d["day"], "balance": d["close_balance"], "min_equity": d["min_equity"]} for d in raw["days"]
        ],
        "baseline": baseline,
        "gates": gates(settings, modules, halves, baseline, equity),
        "skips": raw["skips"],
        "news_coverage": news_coverage,
        "warnings": warnings,
        "trades": trades,
        "settings": settings.model_dump(),
    }
    return clean(report)


def _d(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None


def run_full(params: dict, progress=None) -> dict:
    """Run a complete backtest from job parameters.

    params: settings (dict), overrides (dict), dataset (id), start/end (YYYY-MM-DD),
    random_iterations (int), use_news (bool), variants (list of {name, overrides}).
    """
    base = Settings.model_validate(params.get("settings") or {})
    if params.get("preset"):
        base = base.copy_with(PRESETS[params["preset"]]["overrides"])
    if params.get("overrides"):
        base = base.copy_with(params["overrides"])
    start = date.fromisoformat(params["start"]) if params.get("start") else None
    end = date.fromisoformat(params["end"]) if params.get("end") else None
    meta = datastore.dataset_meta(params["dataset"]) or {"id": params["dataset"]}
    say = progress or (lambda f, m: None)
    say(0.0, "loading data")
    bars = datastore.load_dataset(params["dataset"], start, end)
    if len(bars) == 0:
        raise ValueError("no bars in the selected range")
    news = NewsCalendar.load(base.news) if params.get("use_news", True) else NewsCalendar(base.news, [])
    coverage = news.coverage(int(bars.t[0]), int(bars.t[-1])) if params.get("use_news", True) else None

    variants = params.get("variants") or []
    iterations = int(params.get("random_iterations", base.backtest.random_iterations))
    ticks = params.get("fill_model") == "ticks"

    def simulate(s: Settings, prog):
        if ticks:
            from .ticks import run_tick_backtest

            return run_tick_backtest(s, params["dataset"], start, end, news, progress=prog)
        return run_backtest(s, bars, news, progress=prog)

    if not variants:
        raw = simulate(base, lambda f, m: say(f * 0.7, f"simulating {m}"))
        say(0.7, "random-entry baseline")
        bl = random_baseline(base, bars, raw["trades"], iterations, base.backtest.random_seed,
                             progress=lambda f, m: say(0.7 + f * 0.28, m)) if iterations else {}
        report = build_report(base, raw, bl, coverage, meta)
        report["kind"] = "single"
        return report

    if len(variants) > 24:
        raise ValueError("at most 24 variants per run (freeze the list before testing)")
    rows = []
    for vi, v in enumerate(variants):
        s = base.copy_with(v.get("overrides", {}))
        lo, hi = vi / len(variants), (vi + 1) / len(variants)
        raw = simulate(s, lambda f, m: say(lo + f * (hi - lo) * 0.8, f"{v.get('name')}: {m}"))
        bl = random_baseline(s, bars, raw["trades"], iterations, s.backtest.random_seed) if iterations else {}
        rep = build_report(s, raw, bl, coverage, meta)
        rows.append({
            "name": v.get("name") or f"variant {vi + 1}",
            "overrides": v.get("overrides", {}),
            "summary": rep["summary"],
            "modules": rep["modules"],
            "gates": rep["gates"],
            "halves": rep["halves"],
        })
    return clean({
        "kind": "variants",
        "dataset": meta,
        "start": _d(int(bars.t[0])),
        "end": _d(int(bars.t[-1])),
        "variants": rows,
        "settings": base.model_dump(),
        "warnings": ["Testing many variants inflates the chance of a lucky winner: demand t ≥ 3."],
    })
