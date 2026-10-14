"""The Gold Session Engine terminal: a local web app served by FastAPI.

Open http://127.0.0.1:8050 after ``python -m gse terminal``. Everything about the bot is
shown and controlled here: the live dashboard, the strategy and its schedule, the trade
journal, backtesting, market data and the cTrader connection.

The terminal binds to 127.0.0.1 by default. To reach it on a VPS, use an SSH tunnel
(``ssh -L 8050:127.0.0.1:8050 user@vps``) or set GSE_TERMINAL_PASSWORD before exposing it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..backtest import data as store
from ..backtest.report import PRESETS, clean
from ..backtest.stats import by_module, trade_stats
from ..config import Settings, load_secrets, save_secrets
from ..live.manager import BotManager
from ..news import NewsEvent, parse_csv, to_csv
from ..sessions import SessionClock, dst_note, upcoming_dst_changes

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _session_token(password: str) -> str:
    return hmac.new(password.encode(), b"gse-terminal", hashlib.sha256).hexdigest()


def create_app(manager: BotManager | None = None, autostart: bool = True) -> FastAPI:
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["m"] = manager or BotManager()
        if autostart:
            asyncio.create_task(holder["m"].autostart())
        yield
        await holder["m"].shutdown()

    app = FastAPI(title="Gold Session Engine", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def M() -> BotManager:
        return holder["m"]

    password = os.environ.get("GSE_TERMINAL_PASSWORD")

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if password:
            path = request.url.path
            open_paths = ("/login", "/api/login", "/static/")
            if not path.startswith(open_paths):
                if not hmac.compare_digest(request.cookies.get("gse_session", ""), _session_token(password)):
                    if path.startswith("/api"):
                        return JSONResponse({"detail": "login required"}, status_code=401)
                    return RedirectResponse("/login")
        return await call_next(request)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    async def login(body: dict):
        if not password or not hmac.compare_digest(str(body.get("password", "")), password):
            raise HTTPException(401, "wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie("gse_session", _session_token(password), httponly=True, samesite="strict", max_age=30 * 86400)
        return resp

    # ------------------------------------------------------------------ live state
    @app.get("/api/status")
    async def status():
        return clean({**M().snapshot(), "version": __version__, "server_time": time.time()})

    @app.get("/api/chart")
    async def chart(limit: int = 300):
        return M().chart(limit) or {"bars": []}

    @app.get("/api/events")
    async def events(limit: int = 200, kinds: str | None = None):
        return M().journal.events(limit=limit, kinds=kinds.split(",") if kinds else None)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if password and not hmac.compare_digest(socket.cookies.get("gse_session", ""), _session_token(password)):
            await socket.close(code=4401)
            return
        await socket.accept()
        q = M().subscribe()
        try:
            await socket.send_text(json.dumps({"kind": "snapshot", "payload": clean(M().snapshot())}, default=str))
            last_snapshot = 0.0
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    msg = None
                if msg is not None and msg["kind"] in ("event", "status"):
                    await socket.send_text(json.dumps(msg, default=str))
                now = time.monotonic()
                if now - last_snapshot >= 0.5 and (msg is None or msg["kind"] in ("state", "status", "event")):
                    last_snapshot = now
                    await socket.send_text(json.dumps({"kind": "snapshot", "payload": clean(M().snapshot())}, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            M().unsubscribe(q)

    # ------------------------------------------------------------------ bot control
    @app.post("/api/bot/start")
    async def bot_start(body: dict):
        kind = body.get("kind", "paper")
        try:
            if kind == "replay":
                await M().start_replay(
                    body["dataset"],
                    date.fromisoformat(body["start"]) if body.get("start") else None,
                    date.fromisoformat(body["end"]) if body.get("end") else None,
                    float(body.get("speed", 600)),
                )
            else:
                await M().start_live(int(body["account_id"]), bool(body.get("is_live", False)), kind)
        except (RuntimeError, KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/bot/stop")
    async def bot_stop():
        await M().stop()
        return {"ok": True}

    @app.post("/api/bot/pause")
    async def bot_pause(body: dict):
        M().pause(bool(body.get("paused", True)))
        return {"ok": True}

    @app.post("/api/bot/flatten")
    async def bot_flatten():
        M().flatten()
        return {"ok": True}

    @app.post("/api/bot/close/{ref}")
    async def bot_close(ref: str):
        M().close_position(ref)
        return {"ok": True}

    # ------------------------------------------------------------------ settings and strategy
    @app.get("/api/settings")
    async def get_settings():
        return {"settings": M().settings.model_dump(), "schema": Settings.model_json_schema()}

    @app.put("/api/settings")
    async def put_settings(body: dict):
        try:
            s, notes = M().update_settings(body)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        return {"settings": s.model_dump(), "notes": notes}

    @app.get("/api/schedule")
    async def schedule(day: str | None = None):
        d = date.fromisoformat(day) if day else datetime.now(timezone.utc).date()
        clock = SessionClock(M().settings.sessions)
        w = clock.windows(d)
        return {
            "day": d.isoformat(), "weekday": d.strftime("%A"), "rows": w.rows(), "note": dst_note(d),
            "dst_changes": upcoming_dst_changes(d, 90),
        }

    @app.get("/api/presets")
    async def presets():
        return {k: v["label"] for k, v in PRESETS.items()}

    # ------------------------------------------------------------------ cTrader connection
    @app.get("/api/ctrader/credentials")
    async def get_credentials():
        s = load_secrets()
        return {
            "client_id": s.get("client_id", ""),
            "has_secret": bool(s.get("client_secret")),
            "has_token": bool(s.get("access_token")),
            "has_refresh_token": bool(s.get("refresh_token")),
            "token_expires_at": s.get("token_expires_at"),
            "redirect_uri": M().settings.ctrader.redirect_uri,
            "mock": bool(M().host_override),
        }

    @app.post("/api/ctrader/credentials")
    async def set_credentials(body: dict):
        s = load_secrets()
        for k in ("client_id", "client_secret"):
            if body.get(k):
                s[k] = body[k].strip()
        if body.get("access_token"):
            s["access_token"] = body["access_token"].strip()
            s["token_expires_at"] = int(time.time() + 30 * 86400)
        if body.get("refresh_token"):
            s["refresh_token"] = body["refresh_token"].strip()
        save_secrets(s)
        if body.get("redirect_uri"):
            st = M().settings.model_copy(deep=True)
            st.ctrader.redirect_uri = body["redirect_uri"].strip()
            M().update_settings(st.model_dump())
        return await get_credentials()

    @app.get("/api/ctrader/oauth-url")
    async def oauth_url():
        from ..broker.ctrader.oauth import authorize_url

        s = load_secrets()
        if not s.get("client_id"):
            raise HTTPException(400, "save your app's client id first")
        return {"url": authorize_url(s["client_id"], M().settings.ctrader.redirect_uri)}

    @app.get("/oauth/callback")
    async def oauth_callback(code: str | None = None, error: str | None = None):
        from ..broker.ctrader.oauth import exchange_code

        if error or not code:
            return RedirectResponse(f"/#connection?oauth_error={error or 'no code'}")
        s = load_secrets()
        try:
            tokens = await exchange_code(s["client_id"], s["client_secret"], code, M().settings.ctrader.redirect_uri)
        except Exception as exc:
            return RedirectResponse(f"/#connection?oauth_error={type(exc).__name__}")
        s.update(tokens)
        save_secrets(s)
        return RedirectResponse("/#connection?oauth=ok")

    @app.post("/api/ctrader/refresh-token")
    async def refresh_token():
        from ..broker.ctrader.oauth import refresh_tokens

        s = load_secrets()
        if not s.get("refresh_token"):
            raise HTTPException(400, "no refresh token stored")
        try:
            s.update(await refresh_tokens(s["client_id"], s["client_secret"], s["refresh_token"]))
        except Exception as exc:
            raise HTTPException(400, str(exc))
        save_secrets(s)
        return await get_credentials()

    @app.get("/api/ctrader/accounts")
    async def accounts():
        try:
            return {"accounts": await M().list_accounts()}
        except Exception as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ journal
    def _filters(request: Request) -> dict:
        q = request.query_params
        return {k: q.get(k) or None for k in ("mode", "account", "module", "start", "end", "status")}

    @app.get("/api/trades")
    async def trades(request: Request):
        rows = M().journal.trades(**_filters(request))
        closed = [r for r in rows if r["status"] == "closed" and r["r"] is not None]
        closed.reverse()
        return clean({"trades": rows, "stats": {"all": trade_stats(closed), **by_module(closed)}})

    @app.get("/api/trades.csv")
    async def trades_csv(request: Request):
        return PlainTextResponse(M().journal.trades_csv(**_filters(request)), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=gse-trades.csv"})

    @app.get("/api/journal/accounts")
    async def journal_accounts():
        return M().journal.accounts()

    @app.get("/api/equity")
    async def equity(mode: str | None = None, account: str | None = None, days: int = 30):
        return M().journal.equity_curve(mode, account, int(time.time() - days * 86400))

    @app.get("/api/sizing-check")
    async def sizing_check(mode: str | None = None):
        return M().journal.sizing_check(mode) or {}

    # ------------------------------------------------------------------ backtests
    @app.get("/api/backtests")
    async def backtests():
        return M().jobs.list()

    @app.post("/api/backtests")
    async def start_backtest(body: dict):
        if not body.get("dataset"):
            raise HTTPException(400, "choose a dataset")
        params = dict(body)
        params.setdefault("settings", M().settings.model_dump())
        if params.get("preset") and params["preset"] not in PRESETS:
            raise HTTPException(400, "unknown preset")
        return {"id": M().jobs.start(params)}

    @app.get("/api/backtests/{job_id}/status")
    async def backtest_status(job_id: str):
        return M().jobs.status(job_id)

    @app.get("/api/backtests/{job_id}")
    async def backtest_result(job_id: str):
        res = M().jobs.result(job_id)
        if res is None:
            raise HTTPException(404, "no result (yet)")
        return res

    @app.get("/api/backtests/{job_id}/trades.csv")
    async def backtest_trades_csv(job_id: str):
        import csv
        import io

        res = M().jobs.result(job_id)
        if res is None:
            raise HTTPException(404, "no result")
        buf = io.StringIO()
        cols = ["ref", "module", "side_name", "units", "planned_entry", "entry", "sl_initial", "tp", "exit", "open_ts",
                "close_ts", "exit_reason", "risk_money", "pnl", "commission", "r", "mfe_r", "mae_r", "partial_taken",
                "be_moved", "tag"]
        w = csv.writer(buf)
        w.writerow(["open_utc", "close_utc", *cols])
        for t in res.get("trades", []):
            w.writerow([_utc(t["open_ts"]), _utc(t["close_ts"]), *[t.get(c) for c in cols]])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename=backtest-{job_id}.csv"})

    @app.post("/api/backtests/{job_id}/cancel")
    async def backtest_cancel(job_id: str):
        M().jobs.cancel(job_id)
        return {"ok": True}

    @app.delete("/api/backtests/{job_id}")
    async def backtest_delete(job_id: str):
        M().jobs.delete(job_id)
        return {"ok": True}

    # ------------------------------------------------------------------ market data
    @app.get("/api/datasets")
    async def datasets():
        return store.list_datasets()

    @app.delete("/api/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str):
        try:
            store.delete_dataset(dataset_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/datasets/synthetic")
    async def synthetic(body: dict):
        start = date.fromisoformat(body.get("start", "2018-01-01"))
        end = date.fromisoformat(body.get("end", "2026-09-30"))
        seed = int(body.get("seed", 7))
        ds = body.get("dataset") or "synthetic-XAUUSD"

        def make():
            bars = store.generate_synthetic(start, end, seed=seed)
            store.delete_dataset(ds)
            return store.save_dataset(ds, bars, source="synthetic",
                                      notes="Random data for testing the software only")

        try:
            return await asyncio.to_thread(make)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/datasets/import")
    async def import_csv(request: Request, dataset: str, tz_offset: float = 0.0, source: str = "csv"):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        try:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                tmp.write(chunk)
            tmp.close()
            if size == 0:
                raise HTTPException(400, "empty upload")

            def parse():
                text = Path(tmp.name).read_text(errors="replace")
                bars = store.parse_csv_bars(text, tz_offset_hours=tz_offset)
                if len(bars) == 0:
                    raise ValueError("no bars recognised: check the columns (time, open, high, low, close)")
                return store.save_dataset(dataset, bars, source=source, notes=f"imported CSV ({size // 1024} KB)")

            return await asyncio.to_thread(parse)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    @app.post("/api/datasets/download")
    async def download(body: dict):
        kind = body.get("source")
        if kind not in ("ctrader-trendbars", "ctrader-ticks", "dukascopy"):
            raise HTTPException(400, "source must be ctrader-trendbars, ctrader-ticks or dukascopy")
        return {"id": M().start_download(kind, body)}

    @app.get("/api/downloads")
    async def downloads():
        return M().download_status()

    # ------------------------------------------------------------------ news
    @app.get("/api/news")
    async def news(hours: float = 168, past_hours: float = 24):
        cal = M().news
        now = time.time()
        return {
            "events": cal.upcoming(now - past_hours * 3600, hours + past_hours),
            "total": len(cal.events), "last_refresh": cal.last_refresh, "last_error": cal.last_error,
            "first": cal.events[0].to_dict() if cal.events else None,
            "last": cal.events[-1].to_dict() if cal.events else None,
            "blackout_now": (cal.blackout(now).to_dict() if cal.blackout(now) else None),
        }

    @app.post("/api/news/refresh")
    async def news_refresh():
        added = await M().news.refresh()
        return {"added": added, "error": M().news.last_error}

    @app.post("/api/news/import")
    async def news_import(request: Request):
        text = (await request.body()).decode(errors="replace")
        events = parse_csv(text, source="import")
        if not events:
            raise HTTPException(400, "no events recognised: columns datetime_utc,currency,impact,title")
        added = M().news.merge(events)
        M().news.save()
        return {"added": added, "total": len(M().news.events)}

    @app.post("/api/news/event")
    async def news_add(body: dict):
        try:
            ts = int(datetime.fromisoformat(body["datetime_utc"]).replace(tzinfo=timezone.utc).timestamp())
        except (KeyError, ValueError):
            raise HTTPException(400, "datetime_utc must look like 2026-10-16 12:30")
        ev = NewsEvent(ts, body.get("currency", "USD").upper(), body.get("impact", "High").title(),
                       body.get("title", "Manual event"), "manual")
        M().news.merge([ev])
        M().news.save()
        return ev.to_dict()

    @app.delete("/api/news/event")
    async def news_delete(ts: int, title: str):
        M().news.remove(ts, title)
        M().news.save()
        return {"ok": True}

    @app.get("/api/news.csv")
    async def news_csv():
        return PlainTextResponse(to_csv(M().news.events), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=gse-news.csv"})

    return app


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""


def run_terminal(host: str = "127.0.0.1", port: int = 8050, autostart: bool = True) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("GSE_TERMINAL_PASSWORD"):
        raise SystemExit(
            "Refusing to expose the terminal on the network without a password: set GSE_TERMINAL_PASSWORD "
            "or keep host 127.0.0.1 and use an SSH tunnel."
        )
    uvicorn.run(create_app(autostart=autostart), host=host, port=port, log_level="info")
