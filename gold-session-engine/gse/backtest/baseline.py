"""Random-entry baseline (validation gate 3).

For every real trade of a module a "random twin" is simulated on the same day: a random
entry bar inside the module's window, a coin-flip direction, and the same stop distance,
target distance, break-even/partial plan, costs and session exit. Repeating this many
times gives the distribution of mean R a module would get with no edge in its entries.
A module must clearly beat it: its real mean R should sit above the 95th percentile.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from ..config import Settings
from ..sessions import SessionClock, trading_date
from .data import BarArrays


def _first(mask: np.ndarray) -> int:
    """Index of the first True, or len(mask) if none."""
    if mask.size == 0:
        return 0
    i = int(np.argmax(mask))
    return i if mask[i] else mask.size


class _Twin:
    __slots__ = ("i0", "i1", "j", "d", "tgt", "l1", "frac", "has_partial")


def _window(clock: SessionClock, settings: Settings, module: str, day: date) -> tuple[int, int, int]:
    w = clock.windows(day)
    if module in ("A", "B"):
        start, end = w.london_start, min(w.london_end, w.no_new_trades)
    elif module == "C":
        start, end = w.ny_start, min(w.ny_end, w.no_new_trades)
    else:
        start, end = w.pmfix_start, min(w.pmfix_end, w.no_new_trades)
    exit_ts = w.flat
    if module == "A" and settings.module_a.exit_mode == "server_2100":
        exit_ts = w.server_close
    return start, end, exit_ts


def simulate_twin(bars: BarArrays, i: int, j: int, side: int, d: float, tgt: float, l1: float | None,
                  frac: float, has_partial: bool, slip: float, comm_rt: float, sizing_slip: float,
                  spread_default: float) -> float:
    """R of one trade entered at the close of bar ``i`` and forced out at the close of bar ``j``."""
    o, h, l, c, s = bars.o, bars.h, bars.l, bars.c, bars.s
    sp_i = s[i] if s[i] > 0 else spread_default
    entry = c[i] + sp_i + slip if side > 0 else c[i] - slip
    sl = entry - side * d
    tp = entry + side * tgt
    risk_unit = d + sizing_slip + comm_rt

    if j <= i:
        return -comm_rt / risk_unit
    sl_ = slice(i + 1, j + 1)
    hh, ll, oo, cc = h[sl_], l[sl_], o[sl_], c[sl_]
    ss = s[sl_]
    ss = np.where(ss > 0, ss, spread_default)
    up = cc >= oo

    if side > 0:
        hit_sl = ll <= sl
        hit_tp = hh >= tp
        hit_l1 = hh >= l1 if l1 is not None else None
    else:
        hit_sl = hh + ss >= sl
        hit_tp = ll + ss <= tp
        hit_l1 = ll + ss <= l1 if l1 is not None else None

    def exit_at_close(k: int) -> float:
        px = cc[k]
        return (px - slip) if side > 0 else (px + ss[k] + slip)

    def sl_first_in_bar(k: int) -> bool:
        # long: up bars visit the low first; short: down bars visit the high first
        return bool(up[k]) if side > 0 else not bool(up[k])

    n = hh.size
    k_sl, k_tp = _first(hit_sl), _first(hit_tp)
    realized = 0.0
    weight = 1.0
    stop = sl
    k_l1 = _first(hit_l1) if hit_l1 is not None else n

    if k_l1 < n and k_l1 <= k_sl and k_l1 <= k_tp and not (k_l1 == k_sl and sl_first_in_bar(k_l1)):
        # reached the break-even / partial level first
        if has_partial:
            realized += frac * side * (l1 - entry)
            weight = 1.0 - frac
        stop = entry
        if side > 0:
            hit_sl2 = ll <= stop
        else:
            hit_sl2 = hh + ss >= stop
        hit_sl2[: k_l1] = False
        if hit_tp[k_l1]:
            k_tp2 = k_l1
        else:
            k_tp2 = _first(hit_tp)
        # in the bar that hit the level, the stop can only be hit after the extreme
        if sl_first_in_bar(k_l1):
            # long up bar: low came before the high, so a BE stop in this bar needs the close
            hit_sl2[k_l1] = (cc[k_l1] <= stop) if side > 0 else (cc[k_l1] + ss[k_l1] >= stop)
        k_sl2 = _first(hit_sl2)
        if k_tp2 < n and (k_tp2 < k_sl2 or (k_tp2 == k_sl2 and not sl_first_in_bar(k_tp2))):
            realized += weight * side * (tp - entry)
        elif k_sl2 < n:
            realized += weight * (side * (stop - entry) - slip)
        else:
            realized += weight * side * (exit_at_close(n - 1) - entry)
    else:
        if k_sl < n and (k_sl < k_tp or (k_sl == k_tp and sl_first_in_bar(k_sl))):
            realized = side * (sl - entry) - slip
        elif k_tp < n:
            realized = side * (tp - entry)
        else:
            realized = side * (exit_at_close(n - 1) - entry)
    return (realized - comm_rt) / risk_unit


def random_baseline(settings: Settings, bars: BarArrays, trades: list[dict], iterations: int,
                    seed: int = 42, progress=None) -> dict:
    clock = SessionClock(settings.sessions)
    rng = np.random.default_rng(seed)
    costs = settings.costs
    out = {}
    modules = sorted({t["module"] for t in trades})
    t_arr = bars.t
    for mi, m in enumerate(modules):
        real = [t for t in trades if t["module"] == m]
        twins = []
        for tr in real:
            day = trading_date(tr["open_ts"])
            start, end, exit_ts = _window(clock, settings, m, day)
            # bars whose close falls inside the window
            i0 = int(np.searchsorted(t_arr, start - 60, "left"))
            i1 = int(np.searchsorted(t_arr, end - 60, "right"))
            j = int(np.searchsorted(t_arr, exit_ts - 60, "left"))
            j = min(j, len(t_arr) - 1)
            if i1 <= i0 or j <= i0:
                continue
            tw = _Twin()
            tw.i0, tw.i1, tw.j = i0, i1, j
            d = tr.get("r_dist") or abs(tr["planned_entry"] - tr["sl_initial"])
            tw.d = d
            tw.tgt = abs(tr["tp"] - tr["planned_entry"])
            tw.has_partial = tr.get("partial_price") is not None and tr.get("partial_fraction", 0) > 0
            if tw.has_partial:
                tw.l1 = abs(tr["partial_price"] - tr["planned_entry"])
                tw.frac = tr["partial_fraction"]
            elif tr.get("be_trigger") is not None:
                tw.l1 = abs(tr["be_trigger"] - tr["planned_entry"])
                tw.frac = 0.0
            else:
                tw.l1, tw.frac = None, 0.0
            twins.append(tw)
        if len(twins) < 5:
            out[m] = {"iterations": 0, "note": "fewer than 5 trades"}
            continue
        means = np.empty(iterations)
        for it in range(iterations):
            total = 0.0
            for tw in twins:
                i = int(rng.integers(tw.i0, tw.i1))
                side = 1 if rng.random() < 0.5 else -1
                sp = bars.s[i] if bars.s[i] > 0 else costs.spread_usd
                entry = bars.c[i] + sp + costs.slippage_usd if side > 0 else bars.c[i] - costs.slippage_usd
                l1 = entry + side * tw.l1 if tw.l1 is not None else None
                total += simulate_twin(
                    bars, i, tw.j, side, tw.d, tw.tgt, l1, tw.frac, tw.has_partial, costs.slippage_usd,
                    costs.commission_per_oz_rt, settings.risk.sizing_slippage_usd, costs.spread_usd,
                )
            means[it] = total / len(twins)
            if progress and it % max(1, iterations // 20) == 0:
                progress((mi + it / iterations) / len(modules), f"random baseline {m}: {it}/{iterations}")
        real_mean = float(np.mean([t["r"] for t in real]))
        hist, edges = np.histogram(means, bins=30)
        out[m] = {
            "iterations": iterations,
            "twins": len(twins),
            "real_mean": real_mean,
            "random_mean": float(np.mean(means)),
            "random_sd": float(np.std(means)),
            "random_p95": float(np.percentile(means, 95)),
            "percentile": float(np.mean(means < real_mean) * 100),
            "p_value": float(np.mean(means >= real_mean)),
            "histogram": {"counts": hist.tolist(), "edges": edges.tolist()},
        }
    return out
