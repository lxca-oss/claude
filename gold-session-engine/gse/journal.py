"""Trade journal (SQLite).

Stores every live/paper/replay trade with its module tag, R result and timestamps (for
the validation analysis), the event log shown in the terminal, and periodic equity
snapshots. Open trades are stored with everything needed to resume managing them after
a restart (initial stop, R distance, break-even and partial plan).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import paths
from .models import ClosedTrade, Position
from .sessions import trading_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started INTEGER, ended INTEGER, mode TEXT, account TEXT,
    settings TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    ref TEXT PRIMARY KEY, run_id INTEGER, mode TEXT, account TEXT, day TEXT, status TEXT, module TEXT,
    side INTEGER, units REAL, entry REAL, exit REAL, planned_entry REAL, sl_initial REAL, tp REAL,
    open_ts INTEGER, close_ts INTEGER, pnl REAL, commission REAL, risk_money REAL, r REAL,
    exit_reason TEXT, mfe_r REAL, mae_r REAL, partial_taken INTEGER, be_moved INTEGER, r_dist REAL,
    be_trigger REAL, partial_price REAL, partial_fraction REAL, tag TEXT, notes TEXT, broker_id INTEGER
);
CREATE INDEX IF NOT EXISTS trades_day ON trades(day);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, run_id INTEGER, kind TEXT, message TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS equity (
    ts INTEGER, run_id INTEGER, mode TEXT, account TEXT, balance REAL, equity REAL
);
"""

TRADE_COLS = (
    "ref", "run_id", "mode", "account", "day", "status", "module", "side", "units", "entry", "exit",
    "planned_entry", "sl_initial", "tp", "open_ts", "close_ts", "pnl", "commission", "risk_money", "r",
    "exit_reason", "mfe_r", "mae_r", "partial_taken", "be_moved", "r_dist", "be_trigger", "partial_price",
    "partial_fraction", "tag", "notes", "broker_id",
)


class Journal:
    def __init__(self, path=None):
        self.path = path or paths.journal_path()
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # ---------------------------------------------------------------- runs
    def start_run(self, mode: str, account: str, settings: dict) -> int:
        cur = self._exec("INSERT INTO runs(started, mode, account, settings) VALUES (?,?,?,?)",
                         (int(time.time()), mode, account, json.dumps(settings)))
        return int(cur.lastrowid)

    def end_run(self, run_id: int) -> None:
        self._exec("UPDATE runs SET ended=? WHERE id=?", (int(time.time()), run_id))

    # ---------------------------------------------------------------- trades
    def _upsert(self, row: dict) -> None:
        cols = [c for c in TRADE_COLS if c in row]
        sql = (f"INSERT INTO trades({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT(ref) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'ref')}")
        self._exec(sql, [row[c] for c in cols])

    def record_open(self, p: Position, run_id: int, mode: str, account: str) -> None:
        self._upsert({
            "ref": p.ref, "run_id": run_id, "mode": mode, "account": account,
            "day": trading_date(p.open_ts).isoformat(), "status": "open", "module": p.module, "side": p.side,
            "units": p.initial_units, "entry": p.entry, "planned_entry": p.planned_entry,
            "sl_initial": p.initial_sl, "tp": p.tp, "open_ts": p.open_ts, "risk_money": p.risk_money,
            "r_dist": p.r_dist, "be_trigger": p.be_trigger, "partial_price": p.partial_price,
            "partial_fraction": p.partial_fraction, "tag": p.tag, "notes": json.dumps(p.notes),
            "broker_id": p.broker_id,
        })

    def record_close(self, t: ClosedTrade, run_id: int, mode: str, account: str) -> None:
        d = t.to_dict()
        row = {k: d.get(k) for k in TRADE_COLS if k in d}
        row.update({
            "run_id": run_id, "mode": mode, "account": account, "status": "closed",
            "day": trading_date(t.open_ts).isoformat(), "notes": json.dumps(t.notes),
            "partial_taken": int(t.partial_taken), "be_moved": int(t.be_moved),
        })
        self._upsert(row)

    def accounts(self) -> list[dict]:
        rows = self._exec("SELECT DISTINCT mode, account FROM trades ORDER BY mode, account").fetchall()
        return [dict(r) for r in rows]

    def trade_meta(self, ref: str) -> dict | None:
        cur = self._exec("SELECT * FROM trades WHERE ref=?", (ref,))
        r = cur.fetchone()
        return dict(r) if r else None

    def trades(self, mode: str | None = None, account: str | None = None, module: str | None = None,
               start: str | None = None, end: str | None = None, status: str | None = None,
               limit: int = 5000) -> list[dict]:
        sql, args = "SELECT * FROM trades WHERE 1=1", []
        for col, val in (("mode", mode), ("account", account), ("module", module), ("status", status)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        if start:
            sql += " AND day>=?"
            args.append(start)
        if end:
            sql += " AND day<=?"
            args.append(end)
        sql += " ORDER BY open_ts DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in self._exec(sql, args).fetchall()]
        for r in rows:
            try:
                r["notes"] = json.loads(r["notes"]) if r.get("notes") else []
            except (TypeError, json.JSONDecodeError):
                r["notes"] = [r["notes"]]
        return rows

    def today_trades(self, day: str, mode: str, account: str) -> list[dict]:
        return [t for t in self.trades(mode=mode, account=account, start=day, end=day)]

    def trades_csv(self, **filters) -> str:
        rows = self.trades(**filters)
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["ref", "day", "mode", "account", "module", "side", "units", "planned_entry", "entry", "sl_initial",
                "tp", "exit", "open_utc", "close_utc", "exit_reason", "risk_money", "pnl", "commission", "r",
                "mfe_r", "mae_r", "partial_taken", "be_moved", "tag", "status"]
        w.writerow(cols)
        for r in reversed(rows):
            r = dict(r)
            r["side"] = "BUY" if r["side"] and r["side"] > 0 else "SELL"
            r["open_utc"] = _utc(r.get("open_ts"))
            r["close_utc"] = _utc(r.get("close_ts"))
            w.writerow([r.get(c) for c in cols])
        return buf.getvalue()

    # ---------------------------------------------------------------- events and equity
    def event(self, run_id: int | None, ev: dict) -> None:
        self._exec("INSERT INTO events(ts, run_id, kind, message, data) VALUES (?,?,?,?,?)",
                   (int(ev.get("ts") or time.time()), run_id, ev.get("kind"), ev.get("message"),
                    json.dumps(ev.get("data") or {}, default=str)))

    def events(self, limit: int = 300, kinds: list[str] | None = None, before_id: int | None = None) -> list[dict]:
        sql, args = "SELECT id, ts, run_id, kind, message FROM events WHERE 1=1", []
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if before_id:
            sql += " AND id<?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._exec(sql, args).fetchall()]

    def equity_point(self, run_id: int, mode: str, account: str, balance: float, equity: float) -> None:
        self._exec("INSERT INTO equity(ts, run_id, mode, account, balance, equity) VALUES (?,?,?,?,?,?)",
                   (int(time.time()), run_id, mode, account, balance, equity))

    def equity_curve(self, mode: str | None = None, account: str | None = None, since: int | None = None) -> list[dict]:
        sql, args = "SELECT ts, balance, equity FROM equity WHERE 1=1", []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if account:
            sql += " AND account=?"
            args.append(account)
        if since:
            sql += " AND ts>=?"
            args.append(since)
        sql += " ORDER BY ts"
        return [dict(r) for r in self._exec(sql, args).fetchall()]

    def sizing_check(self, mode: str | None = None) -> dict | None:
        """First trade stopped at its initial stop: actual loss vs the planned risk (doc build note)."""
        sql = ("SELECT * FROM trades WHERE status='closed' AND exit_reason='sl' AND be_moved=0 "
               "AND partial_taken=0")
        args: list = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        sql += " ORDER BY close_ts LIMIT 1"
        r = self._exec(sql, args).fetchone()
        if not r:
            return None
        r = dict(r)
        actual = -r["pnl"]
        return {
            "ref": r["ref"], "module": r["module"], "planned_risk": r["risk_money"], "actual_loss": actual,
            "ratio": actual / r["risk_money"] if r["risk_money"] else None, "day": r["day"],
        }


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
