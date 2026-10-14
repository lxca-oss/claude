"""Market data store and importers.

Datasets are M1 bid bars with an average spread per minute, stored under
``data/market/<dataset id>/`` as one ``.npz`` file per year plus ``meta.json``.

Sources:
* cTrader Open API: M1 trendbars (bid only, spread from settings) or tick data (bid and
  ask ticks, real spread per minute) downloaded with your account;
* Dukascopy public datafeed: M1 bid and ask candles back to 2003 (real spread);
* CSV import: any M1 OHLC file (MT4/MT5/cTrader exports, Dukascopy CSV, ...);
* synthetic: random data for testing the software only, never for judging the strategy.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .. import paths
from ..bars import Bar

FIELDS = ("t", "o", "h", "l", "c", "v", "s")


@dataclass
class BarArrays:
    t: np.ndarray  # int64 unix seconds, bar open
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    s: np.ndarray  # spread $, 0 = unknown

    @classmethod
    def empty(cls) -> "BarArrays":
        return cls(*(np.zeros(0, dtype=np.int64 if f == "t" else np.float64) for f in FIELDS))

    @classmethod
    def from_columns(cls, cols: dict) -> "BarArrays":
        return cls(
            t=np.asarray(cols["t"], dtype=np.int64),
            o=np.asarray(cols["o"], dtype=np.float64),
            h=np.asarray(cols["h"], dtype=np.float64),
            l=np.asarray(cols["l"], dtype=np.float64),
            c=np.asarray(cols["c"], dtype=np.float64),
            v=np.asarray(cols.get("v", np.zeros(len(cols["t"]))), dtype=np.float64),
            s=np.asarray(cols.get("s", np.zeros(len(cols["t"]))), dtype=np.float64),
        )

    def __len__(self) -> int:
        return int(self.t.shape[0])

    def slice(self, start_ts: int | None, end_ts: int | None) -> "BarArrays":
        i0 = 0 if start_ts is None else int(np.searchsorted(self.t, start_ts, "left"))
        i1 = len(self) if end_ts is None else int(np.searchsorted(self.t, end_ts, "left"))
        return BarArrays(*(getattr(self, f)[i0:i1] for f in FIELDS))

    def concat(self, other: "BarArrays") -> "BarArrays":
        merged = BarArrays(*(np.concatenate([getattr(self, f), getattr(other, f)]) for f in FIELDS))
        return merged.dedupe()

    def dedupe(self) -> "BarArrays":
        order = np.argsort(self.t, kind="stable")
        t = self.t[order]
        # keep the last occurrence of each timestamp
        keep = np.ones(len(t), dtype=bool)
        keep[:-1] = t[1:] != t[:-1]
        idx = order[keep]
        return BarArrays(*(getattr(self, f)[idx] for f in FIELDS))

    def iter_bars(self):
        cols = [getattr(self, f).tolist() for f in FIELDS]
        for t, o, h, l, c, v, s in zip(*cols):
            yield Bar(t, o, h, l, c, v, s)

    def spread_known(self) -> float:
        return float(np.mean(self.s > 0)) if len(self) else 0.0


# ---------------------------------------------------------------------------- store
def _ds_dir(dataset_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", dataset_id):
        raise ValueError("dataset id may only contain letters, digits, '.', '_' and '-'")
    return paths.market_dir() / dataset_id


def list_datasets() -> list[dict]:
    out = []
    for d in sorted(paths.market_dir().glob("*/meta.json")):
        try:
            meta = json.loads(d.read_text())
        except json.JSONDecodeError:
            continue
        meta["id"] = d.parent.name
        meta["tick_days"] = _tick_days(d.parent.name)
        out.append(meta)
    return out


def _tick_days(dataset_id: str) -> int:
    p = paths.data_dir() / "ticks" / dataset_id
    return len(list(p.glob("*.npz"))) if p.exists() else 0


def dataset_meta(dataset_id: str) -> dict | None:
    p = _ds_dir(dataset_id) / "meta.json"
    if not p.exists():
        return None
    meta = json.loads(p.read_text())
    meta["id"] = dataset_id
    return meta


def save_dataset(dataset_id: str, arrays: BarArrays, source: str, symbol: str = "XAUUSD",
                 notes: str = "", merge: bool = True) -> dict:
    d = _ds_dir(dataset_id)
    d.mkdir(parents=True, exist_ok=True)
    if len(arrays) == 0:
        raise ValueError("no bars to save")
    arrays = arrays.dedupe()
    years = (arrays.t // 86400 * 86400).astype("datetime64[s]").astype("datetime64[Y]").astype(int) + 1970
    for year in np.unique(years):
        mask = years == year
        part = BarArrays(*(getattr(arrays, f)[mask] for f in FIELDS))
        f = d / f"{int(year)}.npz"
        if merge and f.exists():
            part = _load_file(f).concat(part)
        np.savez_compressed(f, **{k: getattr(part, k) for k in FIELDS})
    return _write_meta(dataset_id, source, symbol, notes)


def _load_file(f: Path) -> BarArrays:
    with np.load(f) as z:
        return BarArrays(*(z[k] for k in FIELDS))


def _write_meta(dataset_id: str, source: str, symbol: str, notes: str) -> dict:
    d = _ds_dir(dataset_id)
    files = sorted(d.glob("*.npz"))
    total, first, last, spread_share = 0, None, None, 0.0
    for f in files:
        a = _load_file(f)
        if len(a) == 0:
            continue
        total += len(a)
        spread_share += float(np.sum(a.s > 0))
        first = int(a.t[0]) if first is None else min(first, int(a.t[0]))
        last = int(a.t[-1]) if last is None else max(last, int(a.t[-1]))
    old = {}
    if (d / "meta.json").exists():
        old = json.loads((d / "meta.json").read_text())
    meta = {
        "source": source or old.get("source", ""),
        "symbol": symbol,
        "timeframe": "M1",
        "price": "bid",
        "bars": total,
        "start": datetime.fromtimestamp(first, timezone.utc).strftime("%Y-%m-%d") if first else None,
        "end": datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%d") if last else None,
        "start_ts": first,
        "end_ts": last,
        "spread_coverage": spread_share / total if total else 0.0,
        "notes": notes or old.get("notes", ""),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "synthetic": source == "synthetic",
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    meta["id"] = dataset_id
    return meta


def load_dataset(dataset_id: str, start: date | None = None, end: date | None = None) -> BarArrays:
    """Load bars with open time in [start, end] (dates inclusive, UTC)."""
    d = _ds_dir(dataset_id)
    if not d.exists():
        raise FileNotFoundError(f"dataset {dataset_id!r} not found")
    parts = []
    for f in sorted(d.glob("*.npz")):
        year = int(f.stem)
        if start and year < start.year or end and year > end.year:
            continue
        parts.append(_load_file(f))
    if not parts:
        return BarArrays.empty()
    arr = parts[0]
    for p in parts[1:]:
        arr = BarArrays(*(np.concatenate([getattr(arr, k), getattr(p, k)]) for k in FIELDS))
    s_ts = _date_ts(start) if start else None
    e_ts = _date_ts(end + timedelta(days=1)) if end else None
    return arr.slice(s_ts, e_ts)


def delete_dataset(dataset_id: str) -> None:
    d = _ds_dir(dataset_id)
    if d.exists():
        shutil.rmtree(d)
    t = paths.data_dir() / "ticks" / dataset_id
    if t.exists():
        shutil.rmtree(t)


def _date_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------- CSV import
_TIME_KEYS = ("timestamp", "time", "datetime", "date_time", "gmt time", "local time", "utc")


_TS_FORMATS = (
    "%Y.%m.%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S",
    "%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
    "%Y%m%d %H%M%S", "%Y%m%d %H:%M:%S", "%Y%m%d %H%M", "%Y%m%d %H:%M",
)


def _parse_ts(value: str, tz_offset_hours: float) -> int:
    """Unix seconds from many timestamp styles; naive times are shifted by ``tz_offset_hours``."""
    v = value.strip().strip('"')
    if re.fullmatch(r"\d{12,13}", v):  # epoch ms
        return int(v) // 1000
    if re.fullmatch(r"\d{9,10}(\.\d+)?", v):  # epoch s
        return int(float(v))
    v = re.sub(r"\s*(GMT|UTC)([+-]\d{2}:?\d{2})?$", "", v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for fmt in _TS_FORMATS:
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                pass
        if dt is None:
            raise ValueError(f"unrecognised timestamp {value!r}")
    if dt.tzinfo is not None:
        return int(dt.timestamp())
    return int(dt.replace(tzinfo=timezone.utc).timestamp() - tz_offset_hours * 3600)


def parse_csv_bars(text: str, tz_offset_hours: float = 0.0, spread_points_divisor: float | None = None) -> BarArrays:
    """Parse an M1 OHLC CSV.

    Recognises a header with time (or date + time), open, high, low, close and optional
    volume, spread, ask_open/ask_close columns. Files without a header are read as
    MetaTrader exports: date, time, open, high, low, close, volume.
    """
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if r and any(x.strip() for x in r)]
    if not rows:
        return BarArrays.empty()
    head = [h.strip().lower().strip("<>") for h in rows[0]]
    has_header = any(h in ("open", "close", "high", "low") for h in head)
    cols: dict[str, list] = {k: [] for k in FIELDS}
    if has_header:
        idx = {h: i for i, h in enumerate(head)}

        def col(*names):
            for n in names:
                if n in idx:
                    return idx[n]
            return None

        i_time = col(*_TIME_KEYS)
        i_date, i_tod = col("date", "day"), col("time", "hour")
        i_o, i_h, i_l, i_c = col("open", "bid_open", "o"), col("high", "bid_high", "h"), col("low", "bid_low", "l"), col("close", "bid_close", "c")
        i_v = col("volume", "tickvol", "tick_volume", "vol", "v")
        i_s = col("spread")
        i_ac = col("ask_close", "askclose", "ask")
        if i_date is not None and i_tod is not None and i_tod != i_date:
            time_of = lambda r: f"{r[i_date]} {r[i_tod]}"  # noqa: E731
        elif i_time is not None:
            time_of = lambda r: r[i_time]  # noqa: E731
        else:
            raise ValueError("CSV needs a time/timestamp column (or date + time)")
        if None in (i_o, i_h, i_l, i_c):
            raise ValueError("CSV needs open, high, low and close columns")
        data_rows = rows[1:]
    else:
        # MetaTrader history export: date,time,open,high,low,close,volume
        time_of = lambda r: f"{r[0]} {r[1]}"  # noqa: E731
        i_o, i_h, i_l, i_c, i_v, i_s, i_ac = 2, 3, 4, 5, 6, None, None
        data_rows = rows

    for r in data_rows:
        try:
            ts = _parse_ts(time_of(r), tz_offset_hours)
            o, h, l, c = float(r[i_o]), float(r[i_h]), float(r[i_l]), float(r[i_c])
        except (ValueError, IndexError):
            continue
        v = float(r[i_v]) if i_v is not None and i_v < len(r) and r[i_v].strip() else 0.0
        s = 0.0
        if i_ac is not None and i_ac < len(r) and r[i_ac].strip():
            s = max(float(r[i_ac]) - c, 0.0)
        elif i_s is not None and i_s < len(r) and r[i_s].strip():
            s = float(r[i_s])
            if spread_points_divisor:
                s /= spread_points_divisor
        cols["t"].append(ts - ts % 60)
        cols["o"].append(o)
        cols["h"].append(h)
        cols["l"].append(l)
        cols["c"].append(c)
        cols["v"].append(v)
        cols["s"].append(s)
    return BarArrays.from_columns(cols).dedupe()


# ---------------------------------------------------------------------------- synthetic data
def _session_vol(minute_of_day_utc: np.ndarray, london_offset: np.ndarray) -> np.ndarray:
    """Relative volatility by time of day: quiet Asia, London open, NY data and COMEX open."""
    lon = (minute_of_day_utc + london_offset * 60) % 1440
    vol = np.full(minute_of_day_utc.shape, 0.55)
    vol[(lon >= 7 * 60) & (lon < 10 * 60)] = 1.15
    vol[(lon >= 10 * 60) & (lon < 13 * 60)] = 0.9
    vol[(lon >= 13 * 60) & (lon < 17 * 60)] = 1.4  # New York morning
    vol[(lon >= 13 * 60 + 30) & (lon < 13 * 60 + 45)] = 2.4  # 08:30 NY data
    vol[(lon >= 17 * 60) & (lon < 21 * 60)] = 0.8
    return vol


def generate_synthetic(start: date, end: date, start_price: float = 1900.0, seed: int = 7,
                       annual_drift: float = 0.06) -> BarArrays:
    """Random-walk gold-like M1 data with session volatility. For testing the software only."""
    rng = np.random.default_rng(seed)
    t0 = _date_ts(start)
    t1 = _date_ts(end + timedelta(days=1))
    t = np.arange(t0, t1, 60, dtype=np.int64)
    dt = t.astype("datetime64[s]")
    dow = ((t // 86400) + 4) % 7  # 0 = Monday
    mod = (t % 86400) // 60
    # market closed from Friday 21:00 UTC to Sunday 22:00 UTC
    open_mask = ~(((dow == 4) & (mod >= 21 * 60)) | (dow == 5) | ((dow == 6) & (mod < 22 * 60)))
    t, mod = t[open_mask], mod[open_mask]
    month = dt[open_mask].astype("datetime64[M]").astype(int) % 12 + 1
    london_offset = np.where((month >= 4) & (month <= 10), 1, 0)
    rel = _session_vol(mod, london_offset)
    # volatility regimes that drift slowly
    regime = np.exp(np.cumsum(rng.normal(0, 0.002, len(t))))
    regime = regime / np.maximum.accumulate(regime) * 0.5 + 0.75
    sigma = 0.00028 * rel * regime  # per-minute log return
    drift = annual_drift / (252 * 1380)
    rets = rng.standard_t(4, len(t)) / math.sqrt(2) * sigma + drift
    logp = math.log(start_price) + np.cumsum(rets)
    close = np.exp(logp)
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.normal(0, 1, (2, len(t)))) * sigma * close * 0.6
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    spread = np.round(0.22 + 0.10 * rng.random(len(t)) + 0.15 * (rel > 2), 2)
    vol = np.round(rel * regime * rng.uniform(20, 60, len(t)))
    r2 = lambda x: np.round(x, 2)  # noqa: E731
    return BarArrays(t=t, o=r2(open_), h=r2(high), l=r2(low), c=r2(close), v=vol, s=spread)
