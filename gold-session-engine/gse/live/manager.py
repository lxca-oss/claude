"""Bot manager: one place that owns the running bot, settings, journal and background jobs.

The terminal talks only to this class. It starts/stops the live, paper or replay
runner, lists the cTrader accounts behind the stored token, runs data downloads, and
fans state updates out to every connected terminal window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date

from ..backtest.jobs import JobManager
from ..config import Settings, load_secrets, load_settings, save_settings
from ..journal import Journal
from ..news import NewsCalendar
from .runner import BaseRunner, LiveRunner, ReplayRunner

log = logging.getLogger(__name__)

LIVE_EDITABLE_PREFIXES = ("module_a.enabled", "module_b.enabled", "module_c.enabled", "module_d.enabled")


class BotManager:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.journal = Journal()
        self.news = NewsCalendar.load(self.settings.news)
        self.runner: BaseRunner | None = None
        self.task: asyncio.Task | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.jobs = JobManager()
        self.downloads: dict[str, dict] = {}
        self.accounts: list[dict] = []
        self.accounts_error: str | None = None
        # GSE_CTRADER_HOST=127.0.0.1:5035 points the bot at a mock server (testing only)
        self.host_override = os.environ.get("GSE_CTRADER_HOST")

    # ---------------------------------------------------------------- pub/sub for the terminal
    def publish(self, kind: str, payload: dict) -> None:
        msg = {"kind": kind, "payload": payload, "ts": time.time()}
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    # ---------------------------------------------------------------- settings
    def update_settings(self, data: dict) -> tuple[Settings, list[str]]:
        """Validate and save new settings. While the bot runs only module on/off switches apply live."""
        new = Settings.model_validate(data)
        notes = []
        if self.running and self.runner.engine is not None:
            old = self.runner.engine.s
            for name in "abcd":
                mod_new, mod_old = getattr(new, f"module_{name}"), getattr(old, f"module_{name}")
                if mod_new.enabled != mod_old.enabled:
                    mod_old.enabled = mod_new.enabled
                    notes.append(f"Module {name.upper()} {'enabled' if mod_new.enabled else 'disabled'} live")
            if new.model_dump(exclude={"module_a", "module_b", "module_c", "module_d"}) != old.model_dump(
                    exclude={"module_a", "module_b", "module_c", "module_d"}) or any(
                    getattr(new, f"module_{n}").model_dump(exclude={"enabled"})
                    != getattr(old, f"module_{n}").model_dump(exclude={"enabled"}) for n in "abcd"):
                notes.append("Other changes are saved and apply the next time the bot starts")
        save_settings(new)
        self.settings = new
        self.news.s = new.news
        self.news.set_events(self.news.events)
        return new, notes

    # ---------------------------------------------------------------- accounts
    def _host_kwargs(self) -> dict:
        if not self.host_override:
            return {}
        host, _, port = self.host_override.partition(":")
        return {"host": host, "port": int(port or 5035), "use_ssl": False}

    async def list_accounts(self) -> list[dict]:
        from ..broker.ctrader.session import CTraderSession

        secrets = load_secrets()
        if not all(secrets.get(k) for k in ("client_id", "client_secret", "access_token")):
            raise RuntimeError("Add your cTrader app credentials and connect (OAuth) first")
        out = []
        for live in (False, True):
            if live and self.host_override:
                continue
            sess = CTraderSession(secrets["client_id"], secrets["client_secret"], secrets["access_token"], live=live,
                                  **self._host_kwargs())
            try:
                await sess.start()
                accounts = await sess.list_accounts()
                for a in accounts:
                    if a.is_live != live:
                        continue
                    row = a.to_dict()
                    try:
                        await sess.authorize_account(a.account_id)
                        info = await sess.account_info()
                        row.update(info.to_dict())
                    except Exception as exc:
                        row["error"] = str(exc)
                    out.append(row)
            except Exception as exc:
                if not live:
                    raise
                log.info("live host: %s", exc)
            finally:
                await sess.close()
        self.accounts = out
        self.accounts_error = None
        return out

    # ---------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start_live(self, account_id: int, is_live: bool, mode: str) -> None:
        if self.running:
            raise RuntimeError("the bot is already running; stop it first")
        if mode == "live" and self.settings.mode != "live":
            raise RuntimeError("live trading is disabled in settings (mode = paper). Enable it deliberately first.")
        kwargs = self._host_kwargs()
        self.runner = LiveRunner(self.settings.model_copy(deep=True), self.journal, self.news, self.publish,
                                 account_id=account_id, is_live=is_live, mode=mode, **kwargs)
        self.task = asyncio.create_task(self.runner.run())
        s = self.settings
        if s.ctrader.account_id != account_id:
            s.ctrader.account_id = account_id
            save_settings(s)

    async def start_replay(self, dataset: str, start: date | None, end: date | None, speed: float) -> None:
        if self.running:
            raise RuntimeError("the bot is already running; stop it first")
        self.runner = ReplayRunner(self.settings.model_copy(deep=True), self.journal, self.news, self.publish,
                                   dataset, start, end, speed)
        self.task = asyncio.create_task(self.runner.run())

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.stop()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        self.publish("status", {"status": "stopped"})

    async def autostart(self) -> None:
        s = self.settings
        if s.autostart and s.ctrader.account_id:
            try:
                accounts = await self.list_accounts()
                acc = next((a for a in accounts if a["account_id"] == s.ctrader.account_id), None)
                if acc is None:
                    raise RuntimeError(f"account {s.ctrader.account_id} not found for this token")
                await self.start_live(acc["account_id"], acc["is_live"], s.mode)
            except Exception as exc:
                log.error("autostart failed: %s", exc)
                self.accounts_error = f"autostart failed: {exc}"

    # ---------------------------------------------------------------- controls
    def pause(self, paused: bool) -> None:
        if self.runner and self.runner.engine:
            self.runner.engine.paused = paused
            self.runner.engine.emit("info", "New entries paused from the terminal" if paused else "Entries resumed")
            if paused:
                for ref in list(self.runner.engine.orders):
                    self.runner.engine.cancel(ref, "paused from the terminal")
        if isinstance(self.runner, ReplayRunner):
            self.runner.paused = paused

    def flatten(self) -> None:
        if self.runner and self.runner.engine:
            self.runner.engine.flatten_all("manual flatten from the terminal")

    def close_position(self, ref: str) -> None:
        if self.runner and self.runner.engine:
            self.runner.engine.close(ref, "closed from the terminal")

    # ---------------------------------------------------------------- state
    def snapshot(self) -> dict:
        r = self.runner
        out = {
            "running": self.running,
            "runner": r.info() if r else None,
            "settings_mode": self.settings.mode,
            "engine": None,
            "errors": [],
            "news": {"last_refresh": self.news.last_refresh, "last_error": self.news.last_error,
                     "events": len(self.news.events)},
        }
        if r and r.engine:
            try:
                out["engine"] = r.engine.snapshot()
            except Exception as exc:
                log.exception("snapshot failed")
                out["engine_error"] = str(exc)
            broker = r.engine.broker
            out["errors"] = list(getattr(broker, "recent_errors", []))[-10:]
            if hasattr(r, "account"):
                out["account"] = r.account
        return out

    def chart(self, limit: int = 300) -> dict | None:
        if self.runner and self.runner.engine:
            return self.runner.engine.chart(limit)
        return None

    def recent_events(self, limit: int = 200) -> list[dict]:
        if self.runner and self.runner.engine:
            return list(self.runner.engine.events)[-limit:]
        return []

    # ---------------------------------------------------------------- data downloads
    def start_download(self, kind: str, params: dict) -> str:
        dl_id = f"{kind}-{int(time.time())}"
        self.downloads[dl_id] = {"id": dl_id, "kind": kind, "state": "running", "progress": 0.0, "message": "starting",
                                 "params": params}

        def progress(frac: float, msg: str) -> None:
            self.downloads[dl_id].update(progress=frac, message=msg)

        async def go():
            try:
                start = date.fromisoformat(params["start"])
                end = date.fromisoformat(params["end"])
                if kind == "dukascopy":
                    from ..backtest.dukascopy import download

                    meta = await download(start, end, params.get("dataset") or "dukascopy-XAUUSD", progress=progress)
                else:
                    meta = await self._download_ctrader(kind, start, end, params, progress)
                self.downloads[dl_id].update(state="done", progress=1.0, message="finished", meta=meta)
            except Exception as exc:
                log.exception("download failed")
                self.downloads[dl_id].update(state="error", message=f"{type(exc).__name__}: {exc}")

        self.downloads[dl_id]["task"] = asyncio.create_task(go())
        return dl_id

    async def _download_ctrader(self, kind: str, start: date, end: date, params: dict, progress) -> dict:
        from ..broker.ctrader.history import download_ticks, download_trendbars
        from ..broker.ctrader.session import CTraderSession

        secrets = load_secrets()
        account_id = int(params.get("account_id") or self.settings.ctrader.account_id or 0)
        if not account_id:
            raise RuntimeError("choose an account first")
        is_live = bool(params.get("is_live", False))
        sess = CTraderSession(secrets["client_id"], secrets["client_secret"], secrets["access_token"], live=is_live,
                              **self._host_kwargs())
        await sess.start()
        try:
            await sess.authorize_account(account_id)
            light, _ = await sess.find_symbol(self.settings.ctrader.symbol)
            ds = params.get("dataset") or f"ctrader-{kind}-{light.symbolName}"
            if kind == "ctrader-ticks":
                return await download_ticks(sess, light.symbolId, start, end, ds, light.symbolName, progress)
            return await download_trendbars(sess, light.symbolId, start, end, ds, light.symbolName, progress)
        finally:
            await sess.close()

    def download_status(self) -> list[dict]:
        return [{k: v for k, v in d.items() if k != "task"} for d in self.downloads.values()]

    async def shutdown(self) -> None:
        await self.stop()
        for d in self.downloads.values():
            t = d.get("task")
            if t and not t.done():
                t.cancel()
