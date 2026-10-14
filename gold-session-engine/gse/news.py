"""High-impact news calendar and the entry blackout filter.

Rule: no new entries from 5 minutes before to 5 minutes after high-impact USD news, and
until 15 minutes after NFP, CPI and FOMC.

Sources:
* live: the Forex Factory weekly JSON feed (``news.feed_url``), refreshed hourly and
  merged into ``data/news/events.csv`` so history builds up over time;
* history for backtests: import a CSV (``datetime_utc,currency,impact,title``);
* manual events added in the terminal.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import NewsSettings
from . import paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsEvent:
    ts: int
    currency: str
    impact: str
    title: str
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "time_utc": datetime.fromtimestamp(self.ts, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "currency": self.currency,
            "impact": self.impact,
            "title": self.title,
            "source": self.source,
        }


def _parse_time(value: str) -> int:
    value = value.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_forex_factory(payload: str | bytes) -> list[NewsEvent]:
    """Parse the Forex Factory calendar JSON (list of {title, country, date, impact})."""
    data = json.loads(payload)
    out = []
    for row in data:
        try:
            ts = _parse_time(row["date"])
        except (KeyError, ValueError):
            continue
        out.append(
            NewsEvent(
                ts=ts,
                currency=str(row.get("country", "")).upper(),
                impact=str(row.get("impact", "")).title(),
                title=str(row.get("title", "")),
                source="forexfactory",
            )
        )
    return out


def parse_csv(text: str, source: str = "csv") -> list[NewsEvent]:
    """CSV with a header containing datetime_utc (or date+time), currency, impact, title."""
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if "datetime_utc" in row:
            stamp = row["datetime_utc"]
        elif "date" in row and "time" in row:
            stamp = f"{row['date']} {row['time']}"
        else:
            stamp = row.get("datetime", "")
        try:
            ts = _parse_time(stamp)
        except ValueError:
            continue
        out.append(
            NewsEvent(
                ts=ts,
                currency=row.get("currency", row.get("country", "")).upper(),
                impact=row.get("impact", "High").title(),
                title=row.get("title", row.get("event", "")),
                source=row.get("source") or source,
            )
        )
    return out


def to_csv(events: list[NewsEvent]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["datetime_utc", "currency", "impact", "title", "source"])
    for e in events:
        w.writerow([
            datetime.fromtimestamp(e.ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            e.currency, e.impact, e.title, e.source,
        ])
    return buf.getvalue()


class NewsCalendar:
    def __init__(self, settings: NewsSettings, events: list[NewsEvent] | None = None):
        self.s = settings
        self._events: list[NewsEvent] = []
        self._windows: list[tuple[int, int, NewsEvent]] = []
        self._starts: list[int] = []
        self.last_refresh: float | None = None
        self.last_error: str | None = None
        self.set_events(events or [])

    # -- storage ------------------------------------------------------------------
    @staticmethod
    def store_path() -> Path:
        return paths.news_dir() / "events.csv"

    @classmethod
    def load(cls, settings: NewsSettings) -> "NewsCalendar":
        p = cls.store_path()
        events = parse_csv(p.read_text(), source="store") if p.exists() else []
        return cls(settings, events)

    def save(self) -> None:
        self.store_path().write_text(to_csv(self._events))

    # -- data ---------------------------------------------------------------------
    def set_events(self, events: list[NewsEvent]) -> None:
        uniq = {(e.ts, e.currency, e.title): e for e in events}
        self._events = sorted(uniq.values(), key=lambda e: e.ts)
        self._rebuild()

    def merge(self, events: list[NewsEvent]) -> int:
        before = len(self._events)
        self.set_events(self._events + list(events))
        return len(self._events) - before

    def remove(self, ts: int, title: str) -> None:
        self.set_events([e for e in self._events if not (e.ts == ts and e.title == title)])

    @property
    def events(self) -> list[NewsEvent]:
        return self._events

    def is_major(self, e: NewsEvent) -> bool:
        title = e.title.lower()
        return any(k.lower() in title for k in self.s.major_keywords)

    def relevant(self, e: NewsEvent) -> bool:
        return e.currency.upper() in {c.upper() for c in self.s.currencies} and e.impact.title() in {
            i.title() for i in self.s.impacts
        }

    def _rebuild(self) -> None:
        wins = []
        for e in self._events:
            if not self.relevant(e):
                continue
            after = self.s.major_after_minutes if self.is_major(e) else self.s.after_minutes
            wins.append((e.ts - self.s.before_minutes * 60, e.ts + after * 60, e))
        wins.sort(key=lambda w: w[0])
        self._windows = wins
        self._starts = [w[0] for w in wins]

    # -- queries ------------------------------------------------------------------
    def blackout(self, ts: float) -> NewsEvent | None:
        """The event whose blackout window covers ``ts`` (or None)."""
        if not self.s.enabled or not self._windows:
            return None
        i = bisect.bisect_right(self._starts, ts)
        # windows are short, so only a few earlier starts can still be open
        for j in range(i - 1, max(i - 8, -1), -1):
            start, end, e = self._windows[j]
            if start <= ts < end:
                return e
        return None

    def next_event(self, ts: float) -> NewsEvent | None:
        for start, end, e in self._windows[bisect.bisect_left(self._starts, ts - 3600):]:
            if e.ts >= ts:
                return e
        return None

    def upcoming(self, ts: float, hours: float = 48) -> list[dict]:
        out = []
        for e in self._events:
            if ts - 3600 <= e.ts <= ts + hours * 3600:
                d = e.to_dict()
                d["relevant"] = self.relevant(e)
                d["major"] = self.is_major(e)
                out.append(d)
        return out

    def coverage(self, start_ts: int, end_ts: int) -> float:
        """Share of weeks in the range with at least one relevant event (a proxy for data coverage)."""
        weeks = max(1, (end_ts - start_ts) // (7 * 86400))
        covered = {(w[2].ts - start_ts) // (7 * 86400) for w in self._windows if start_ts <= w[2].ts < end_ts}
        return min(1.0, len(covered) / weeks)

    # -- live refresh ---------------------------------------------------------------
    async def refresh(self) -> int:
        import time

        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(self.s.feed_url, headers={"User-Agent": "gold-session-engine"})
                r.raise_for_status()
            added = self.merge(parse_forex_factory(r.content))
            self.save()
            self.last_refresh = time.time()
            self.last_error = None
            return added
        except Exception as exc:  # network errors must never stop the bot
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("News refresh failed: %s", self.last_error)
            return 0

