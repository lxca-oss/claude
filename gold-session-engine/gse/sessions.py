"""Session clock.

All strategy windows are defined in London or New York local time. cTrader bars are
UTC and daylight saving moves every window by an hour twice a year (and London and New
York switch on different weekends), so every window is converted per day with the IANA
timezone database instead of fixed UTC offsets.

A "trading day" is the London calendar date. Every window of the strategy (00:00 London
to 11:30 New York) falls inside one London date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .config import SessionSettings, parse_hhmm

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
SYDNEY = ZoneInfo("Australia/Sydney")


def local_to_ts(day: date, t: time, tz: ZoneInfo) -> int:
    """Unix seconds for a wall-clock time on ``day`` in ``tz``."""
    return int(datetime.combine(day, t, tzinfo=tz).timestamp())


def ts_to_dt(ts: float, tz=UTC) -> datetime:
    return datetime.fromtimestamp(ts, tz)


@lru_cache(maxsize=8192)
def _trading_date_hour(hour_index: int) -> date:
    return ts_to_dt(hour_index * 3600, LONDON).date()


def trading_date(ts: float) -> date:
    """London calendar date of ``ts``.

    London's UTC offset is always a whole number of hours, so the date is constant within
    each UTC hour and can be cached per hour (this runs once per bar in backtests).
    """
    return _trading_date_hour(int(ts) // 3600)


@dataclass(frozen=True)
class DayWindows:
    day: date
    asian_start: int
    asian_end: int
    london_start: int
    london_end: int
    vwap_anchor: int
    ny_start: int
    ny_end: int
    pmfix_start: int
    pmfix_end: int
    move_anchor: int
    no_new_trades: int
    flat: int
    server_close: int

    @property
    def weekday(self) -> int:
        return self.day.weekday()  # Monday = 0

    @property
    def is_weekend(self) -> bool:
        return self.weekday >= 5

    def rows(self) -> list[dict]:
        """Schedule table for the terminal: each window in UTC, London, New York and Sydney."""

        def fmt(ts: int, tz) -> str:
            return ts_to_dt(ts, tz).strftime("%H:%M")

        def row(name, start, end, defined_in):
            return {
                "window": name,
                "defined_in": defined_in,
                "start_ts": start,
                "end_ts": end,
                "utc": f"{fmt(start, UTC)}–{fmt(end, UTC)}",
                "london": f"{fmt(start, LONDON)}–{fmt(end, LONDON)}",
                "new_york": f"{fmt(start, NEW_YORK)}–{fmt(end, NEW_YORK)}",
                "sydney": f"{fmt(start, SYDNEY)}–{fmt(end, SYDNEY)}",
            }

        return [
            row("Asian range", self.asian_start, self.asian_end, "London"),
            row("Module A + B (London)", self.london_start, self.london_end, "London"),
            row("Module C (New York)", self.ny_start, self.ny_end, "New York"),
            row("Module D (PM fix)", self.pmfix_start, self.pmfix_end, "New York"),
            row("No new trades / flat", self.no_new_trades, self.flat, "New York"),
        ]


class SessionClock:
    def __init__(self, settings: SessionSettings):
        self.s = settings
        self._t = {
            name: parse_hhmm(getattr(settings, name))
            for name in (
                "asian_start", "asian_end", "london_start", "london_end", "ny_start", "ny_end",
                "pmfix_start", "pmfix_end", "move_anchor_ny", "no_new_trades_ny", "flat_ny",
                "server_close_ny",
            )
        }
        self._cache: dict[date, DayWindows] = {}

    def windows(self, day: date) -> DayWindows:
        w = self._cache.get(day)
        if w is None:
            t = self._t
            w = DayWindows(
                day=day,
                asian_start=local_to_ts(day, t["asian_start"], LONDON),
                asian_end=local_to_ts(day, t["asian_end"], LONDON),
                london_start=local_to_ts(day, t["london_start"], LONDON),
                london_end=local_to_ts(day, t["london_end"], LONDON),
                vwap_anchor=local_to_ts(day, t["london_start"], LONDON),
                ny_start=local_to_ts(day, t["ny_start"], NEW_YORK),
                ny_end=local_to_ts(day, t["ny_end"], NEW_YORK),
                pmfix_start=local_to_ts(day, t["pmfix_start"], NEW_YORK),
                pmfix_end=local_to_ts(day, t["pmfix_end"], NEW_YORK),
                move_anchor=local_to_ts(day, t["move_anchor_ny"], NEW_YORK),
                no_new_trades=local_to_ts(day, t["no_new_trades_ny"], NEW_YORK),
                flat=local_to_ts(day, t["flat_ny"], NEW_YORK),
                server_close=local_to_ts(day, t["server_close_ny"], NEW_YORK),
            )
            if len(self._cache) > 4000:
                self._cache.clear()
            self._cache[day] = w
        return w

    def windows_at(self, ts: float) -> DayWindows:
        return self.windows(trading_date(ts))


@lru_cache(maxsize=64)
def dst_note(day: date) -> str:
    """Explain the London/New York offset on a given day (it is 4h or 5h)."""
    noon = time(12, 0)
    lon = datetime.combine(day, noon, tzinfo=LONDON)
    ny = datetime.combine(day, noon, tzinfo=NEW_YORK)
    gap = int((ny - lon).total_seconds() // 3600)
    lon_name = lon.tzname()
    ny_name = ny.tzname()
    syd_name = datetime.combine(day, noon, tzinfo=SYDNEY).tzname()
    note = f"London {lon_name}, New York {ny_name}, Sydney {syd_name}; New York is {gap}h behind London"
    if gap != 5:
        note += " (mixed DST week: London and New York change clocks on different dates)"
    return note


def upcoming_dst_changes(start: date, days: int = 60) -> list[dict]:
    """Dates within ``days`` where London, New York or Sydney change their UTC offset."""
    out = []
    zones = {"London": LONDON, "New York": NEW_YORK, "Sydney": SYDNEY}
    for name, tz in zones.items():
        prev = None
        for i in range(days + 1):
            d = start + timedelta(days=i)
            off = datetime.combine(d, time(12), tzinfo=tz).utcoffset()
            if prev is not None and off != prev:
                out.append({"zone": name, "date": d.isoformat(), "new_offset_hours": off.total_seconds() / 3600})
            prev = off
    return sorted(out, key=lambda r: r["date"])
