"""$20 round-level helpers.

Research finding: gold approaching a $20 level with momentum is more likely to reject
than break, typically stalling about $1 short. The effect is too small to trade on its
own, so it is used as a placement rule:

* targets: if the next $20 level is closer than the planned target, the target moves to
  $1 short of that level;
* stops: a stop may not sit within $2 on the near side of a level, and must be at least
  $2.5 beyond it, so any stop in the zone (level - 2.5, level + 2] (long) is pushed to
  level - 2.5 (mirrored for shorts).
"""

from __future__ import annotations

import math

from .config import LevelSettings

EPS = 1e-9


def level_above(price: float, step: float = 20.0) -> float:
    """First level strictly above ``price``."""
    return (math.floor(price / step + EPS) + 1) * step


def level_below(price: float, step: float = 20.0) -> float:
    """First level strictly below ``price``."""
    return (math.ceil(price / step - EPS) - 1) * step


def nearest_level(price: float, step: float = 20.0) -> float:
    return round(price / step) * step


def levels_between(lo: float, hi: float, step: float = 20.0) -> list[float]:
    first = math.ceil(lo / step - EPS) * step
    out = []
    x = first
    while x <= hi + EPS:
        out.append(x)
        x += step
    return out


def adjust_target(side: int, entry: float, target: float, cfg: LevelSettings) -> tuple[float, bool]:
    """Pull the target in to ``offset`` short of the next level if that level comes first.

    ``side`` is +1 for long and -1 for short. Returns (target, adjusted?).
    """
    if side > 0:
        lvl = level_above(entry, cfg.step)
        if lvl <= target + EPS:
            return lvl - cfg.target_offset, True
    else:
        lvl = level_below(entry, cfg.step)
        if lvl >= target - EPS:
            return lvl + cfg.target_offset, True
    return target, False


def adjust_stop(side: int, entry: float, stop: float, cfg: LevelSettings) -> tuple[float, bool]:
    """Push a stop that sits too close to a level to ``stop_beyond`` past it.

    Only levels between the stop and the entry (the ones price must cross to reach the
    stop) matter. Pushing only ever widens the stop. Returns (stop, adjusted?).
    """
    step = cfg.step
    if side > 0:
        # zone for level L below entry: (L - beyond, L + near]
        for lvl in (nearest_level(stop, step) - step, nearest_level(stop, step), nearest_level(stop, step) + step):
            if lvl >= entry - EPS:
                continue
            if lvl - cfg.stop_beyond + EPS < stop <= lvl + cfg.stop_near_zone + EPS:
                return lvl - cfg.stop_beyond, True
    else:
        for lvl in (nearest_level(stop, step) - step, nearest_level(stop, step), nearest_level(stop, step) + step):
            if lvl <= entry + EPS:
                continue
            if lvl - cfg.stop_near_zone - EPS <= stop < lvl + cfg.stop_beyond - EPS:
                return lvl + cfg.stop_beyond, True
    return stop, False
