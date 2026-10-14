"""Runners: the engine on live cTrader prices (live or paper) and on historical replays.

LiveRunner
    connects to the cTrader Open API, warms the indicators up with recent M1 history,
    builds closed M1 bars from the price stream, and feeds them to the engine. In
    ``live`` mode orders go to the account through CTraderBroker; in ``paper`` mode the
    simulator fills them on the live quotes (forward testing without sending orders).
    Disconnects are retried with backoff; missed bars are back-filled from the server.

ReplayRunner
    plays a stored dataset through the engine and simulator at a chosen speed so the
    terminal can be watched working without an account.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Callable

from ..bars import Bar
from ..broker.sim import SimBroker
from ..config import Settings, load_secrets, save_secrets
from ..engine import StrategyEngine
from ..journal import Journal
from ..news import NewsCalendar
from ..sessions import trading_date

log = logging.getLogger(__name__)

Publish = Callable[[str, dict], None]


class BaseRunner:
    mode = "?"

    def __init__(self, settings: Settings, journal: Journal, news: NewsCalendar, publish: Publish):
        self.s = settings
        self.journal = journal
        self.news = news
        self.publish = publish
        self.engine: StrategyEngine | None = None
        self.status = "starting"
        self.detail = ""
        self.account_label = ""
        self.account: dict = {}
        self.run_id: int | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_publish = 0.0
        self._last_equity = 0.0

    # events coming out of the engine -> journal + terminal
    def on_engine_event(self, kind: str, message: str, ev: dict) -> None:
        if not self.engine or not self.engine.trading:
            return
        try:
            self.journal.event(self.run_id, ev)
            data = ev.get("data") or {}
            if kind == "fill" and "position" in data and self.engine:
                p = self.engine.positions.get(data["position"]["ref"])
                if p is not None:
                    self.journal.record_open(p, self.run_id, self.mode, self.account_label)
            elif kind == "close" and self.engine and self.engine.closed:
                self.journal.record_close(self.engine.closed[-1], self.run_id, self.mode, self.account_label)
        except Exception:
            log.exception("journal write failed")
        self.publish("event", ev)

    def set_status(self, status: str, detail: str = "") -> None:
        self.status, self.detail = status, detail
        self.publish("status", {"status": status, "detail": detail})
        log.info("Runner %s: %s %s", self.mode, status, detail)

    def maybe_publish_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_publish > 0.5:
            self._last_publish = now
            self.publish("state", {})
        if self.engine and now - self._last_equity > 60:
            self._last_equity = now
            try:
                self.journal.equity_point(self.run_id, self.mode, self.account_label,
                                          self.engine.broker.balance(), self.engine.broker.equity())
            except Exception:
                log.exception("equity snapshot failed")

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self.run_id is not None:
            self.journal.end_run(self.run_id)
        self.set_status("stopped")

    def info(self) -> dict:
        return {"mode": self.mode, "status": self.status, "detail": self.detail, "account": self.account}


# =============================================================================== live / paper
class LiveRunner(BaseRunner):
    def __init__(self, settings: Settings, journal: Journal, news: NewsCalendar, publish: Publish,
                 account_id: int, is_live: bool, mode: str = "paper", host: str | None = None,
                 port: int | None = None, use_ssl: bool = True):
        super().__init__(settings, journal, news, publish)
        assert mode in ("live", "paper")
        self.mode = mode
        self.account_id = account_id
        self.is_live = is_live
        self.host, self.port, self.use_ssl = host, port, use_ssl
        self.session = None
        self.broker = None
        self.symbol_id: int | None = None
        self._disconnected = asyncio.Event()
        self._cur: Bar | None = None
        self._cur_spreads: list[float] = []
        self._last_closed_t = 0
        self._bars: asyncio.Queue[Bar] = asyncio.Queue()
        self._bid: float | None = None
        self._ask: float | None = None
        self.last_quote_at = 0.0

    async def run(self) -> None:
        backoff = 2
        self._tasks.append(asyncio.create_task(self._bar_consumer()))
        self._tasks.append(asyncio.create_task(self._bar_timer()))
        self._tasks.append(asyncio.create_task(self._housekeeping()))
        while not self._stop.is_set():
            try:
                await self._connect()
                backoff = 2
                self.set_status("running", f"{self.mode} on {self.account_label}")
                stop_wait = asyncio.create_task(self._stop.wait())
                disc_wait = asyncio.create_task(self._disconnected.wait())
                await asyncio.wait({stop_wait, disc_wait}, return_when=asyncio.FIRST_COMPLETED)
                stop_wait.cancel()
                disc_wait.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("live runner error")
                self.set_status("error", f"{type(exc).__name__}: {exc}")
                if self.engine:
                    self.engine.emit("error", f"Connection problem: {exc}")
            if self._stop.is_set():
                break
            if self.session is not None:
                try:
                    await self.session.client.close()
                except Exception:
                    pass
            self.set_status("reconnecting", f"retry in {backoff}s")
            try:
                await asyncio.wait_for(self._stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        if self.session is not None:
            await self.session.close()

    async def stop(self) -> None:
        await super().stop()
        if self.session is not None:
            try:
                await self.session.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- connection
    async def _ensure_token(self, secrets: dict) -> dict:
        exp = secrets.get("token_expires_at") or 0
        if secrets.get("refresh_token") and exp and exp - time.time() < 3 * 86400:
            from ..broker.ctrader.oauth import refresh_tokens

            try:
                new = await refresh_tokens(secrets["client_id"], secrets["client_secret"], secrets["refresh_token"])
                secrets.update(new)
                save_secrets(secrets)
                log.info("Access token refreshed")
            except Exception as exc:
                log.warning("Token refresh failed: %s", exc)
        return secrets

    async def _connect(self) -> None:
        from ..broker.ctrader import protocol as P
        from ..broker.ctrader.broker import CTraderBroker
        from ..broker.ctrader.proto import model
        from ..broker.ctrader.session import CTraderSession

        self.set_status("connecting")
        secrets = await self._ensure_token(load_secrets())
        for k in ("client_id", "client_secret", "access_token"):
            if not secrets.get(k):
                raise RuntimeError(f"missing {k}: connect your cTrader app in the Connection tab")
        self._disconnected.clear()
        kwargs = {}
        if self.host:
            kwargs.update(host=self.host, port=self.port or P.PORT, use_ssl=self.use_ssl)
        session = CTraderSession(secrets["client_id"], secrets["client_secret"], secrets["access_token"],
                                 live=self.is_live, **kwargs)
        await session.start()
        await session.authorize_account(self.account_id)
        self.session = session
        session.client.on_disconnect = lambda reason: self._disconnected.set()
        info = await session.account_info()
        self.account = info.to_dict()
        self.account_label = f"{'live' if info.is_live else 'demo'}-{info.login or self.account_id}"
        light, sym = await session.find_symbol(self.s.ctrader.symbol)
        self.symbol_id = light.symbolId

        now_ms = int(time.time() * 1000)
        history = await session.trendbars(self.symbol_id, now_ms - 4 * 86_400_000, now_ms, model.M1)
        ref_price = history[-1].c if history else None
        spec = session.symbol_spec(light, sym, self.s.ctrader.commission_per_oz_rt, reference_price=ref_price)
        self.account["symbol"] = {
            "name": spec.name, "digits": spec.digits, "contract_size": spec.contract_size,
            "min_units": spec.min_units, "step_units": spec.step_units,
            "commission_per_oz_rt": spec.commission_per_unit_rt,
        }
        await self._setup_conversion(session, light, info)

        first_time = self.engine is None
        if first_time:
            if self.mode == "live":
                self.broker = CTraderBroker(
                    session, self.symbol_id, spec, self.s.ctrader.label, balance=info.balance,
                    money_digits=info.money_digits, notify=self._notify, trade_meta=self.journal.trade_meta)
            else:
                self.broker = SimBroker(spec, self.s.costs, info.balance)
            self.engine = StrategyEngine(self.s, self.broker, self.news, emit=self.on_engine_event)
            self.run_id = self.journal.start_run(self.mode, self.account_label, self.s.model_dump())
            self.set_status("warming up", f"{len(history)} M1 bars")
            self._warmup(history, now_ms // 1000)
            today = trading_date(time.time()).isoformat()
            self.engine.restore_today(self.journal.today_trades(today, self.mode, self.account_label))
            if self.mode == "live":
                for p in await self.broker.restore():
                    self.engine.adopt_position(p)
        else:
            if self.mode == "live":
                await self._rebind_live_broker(session)
            missed = [b for b in history if b.t > self._last_closed_t and b.t + 60 <= now_ms // 1000]
            if missed:
                self._feed(missed, trading=False)
                self.engine.emit("info", f"Back-filled {len(missed)} missed M1 bars after reconnecting")

        session.client.on(model.PROTO_OA_SPOT_EVENT, self._on_spot)
        await session.subscribe_spots([self.symbol_id])
        await session.subscribe_live_bars(self.symbol_id, model.M1)
        self.engine.emit("info", f"Connected to {info.broker} account {info.login} ({info.currency}), mode {self.mode}")

    async def _setup_conversion(self, session, light, info) -> None:
        """Track the USD -> account currency rate for non-USD accounts."""
        self._conv: list = []
        if info.currency.upper() == "USD" or not light.HasField("quoteAssetId"):
            return
        trader = await session.trader()
        try:
            chain = await session.conversion_chain(light.quoteAssetId, trader.depositAssetId)
        except Exception as exc:
            self._notify("error", f"No conversion chain to {info.currency}: {exc}; P&L shown in USD")
            return
        self._conv = [{"id": s.symbolId, "base": s.baseAssetId, "quote": s.quoteAssetId, "bid": None} for s in chain]
        await session.subscribe_spots([c["id"] for c in self._conv])
        self._conv_from = light.quoteAssetId

    def _update_conversion(self, symbol_id: int, bid: float | None) -> None:
        if not getattr(self, "_conv", None) or bid is None:
            return
        for c in self._conv:
            if c["id"] == symbol_id:
                c["bid"] = bid
        if any(c["bid"] is None for c in self._conv):
            return
        rate, asset = 1.0, self._conv_from
        for c in self._conv:
            if c["base"] == asset:
                rate *= c["bid"]
                asset = c["quote"]
            else:
                rate /= c["bid"]
                asset = c["base"]
        if self.engine:
            self.engine.symbol.quote_to_account = rate

    async def _rebind_live_broker(self, session) -> None:
        from ..broker.ctrader.proto import model

        b = self.broker
        b.session, b.client, b.account_id = session, session.client, session.account_id
        b.client.on(model.PROTO_OA_EXECUTION_EVENT, b._on_execution)
        b.client.on(model.PROTO_OA_ORDER_ERROR_EVENT, b._on_order_error)
        b.client.on(model.PROTO_OA_TRADER_UPDATE_EVENT, b._on_trader_update)
        res = await session.reconcile()
        live_ids = {p.positionId for p in res.position}
        for ref, p in list(b.positions.items()):
            if p.broker_id not in live_ids:
                # closed by SL/TP while we were offline: book it at the last known price
                from ..broker.base import make_closed_trade

                b.positions.pop(ref, None)
                price = p.sl if p.sl is not None else p.entry
                gross = (price - p.entry) * p.side * p.units * b.symbol.quote_to_account
                trade = make_closed_trade(p, int(time.time()), price, p.units, "closed while disconnected (approx.)",
                                          gross, 0.0)
                self.engine.on_position_closed(p, trade)
        open_ids = {o.orderId for o in res.order}
        for ref, o in list(b.orders.items()):
            if o.broker_id and o.broker_id not in open_ids:
                b.orders.pop(ref, None)
                self.engine.on_order_cancelled(o, "no longer on the server after reconnect", int(time.time()))
        await b.refresh_balance()

    def _notify(self, level: str, msg: str) -> None:
        if self.engine:
            self.engine.emit("error" if level == "error" else "info", msg)

    # ---------------------------------------------------------------- bars
    def _warmup(self, history: list[Bar], now_s: int) -> None:
        closed = [b for b in history if b.t + 60 <= now_s]
        self._feed(closed, trading=False)

    def _feed(self, bars: list[Bar], trading: bool) -> None:
        """Feed closed bars; with ``trading=False`` they only update indicators and state."""
        eng = self.engine
        prev = eng.trading
        eng.trading = trading
        try:
            for b in bars:
                if b.t <= self._last_closed_t:
                    continue
                if b.s <= 0:
                    b.s = self.s.costs.spread_usd
                eng.on_bar(b)
                self._last_closed_t = b.t
        finally:
            eng.trading = prev

    def _on_spot(self, pt, ev) -> None:
        from ..broker.ctrader import protocol as P

        if ev.symbolId != self.symbol_id:
            self._update_conversion(ev.symbolId, ev.bid / P.PRICE_SCALE if ev.HasField("bid") else None)
            return
        if ev.HasField("bid"):
            self._bid = ev.bid / P.PRICE_SCALE
        if ev.HasField("ask"):
            self._ask = ev.ask / P.PRICE_SCALE
        if self._bid is None or self._ask is None:
            return
        ts = int(ev.timestamp / 1000) if ev.HasField("timestamp") else int(time.time())
        self.last_quote_at = time.time()
        if self.mode == "live":
            self.broker.on_quote(ts, self._bid, self._ask)
        else:
            self.broker.process_quote(ts, self._bid, self._ask)
        self.engine.on_quote(ts, self._bid, self._ask)

        minute = ts - ts % 60
        if minute <= self._last_closed_t:
            return
        if self._cur is not None and minute > self._cur.t:
            self._close_current()
        if self._cur is None:
            self._cur = Bar(minute, self._bid, self._bid, self._bid, self._bid, 0.0, 0.0)
            self._cur_spreads = []
        c = self._cur
        c.h = max(c.h, self._bid)
        c.l = min(c.l, self._bid)
        c.c = self._bid
        c.v += 1
        self._cur_spreads.append(self._ask - self._bid)
        for tb in ev.trendbar:
            if tb.period == 1 and tb.utcTimestampInMinutes * 60 == c.t:  # M1: server's own bar is authoritative
                sb = P.decode_trendbar(tb)
                c.o, c.h, c.l, c.c, c.v = sb.o, sb.h, sb.l, sb.c, sb.v
        self.maybe_publish_state()

    def _close_current(self) -> None:
        c = self._cur
        if c is None:
            return
        c.s = sum(self._cur_spreads) / len(self._cur_spreads) if self._cur_spreads else self.s.costs.spread_usd
        self._cur = None
        self._bars.put_nowait(c)

    async def _bar_timer(self) -> None:
        """Close the current minute if no tick arrives after it ends (quiet markets)."""
        while not self._stop.is_set():
            await asyncio.sleep(1)
            if self._cur is not None and time.time() >= self._cur.t + 62:
                self._close_current()

    async def _bar_consumer(self) -> None:
        from ..broker.ctrader.proto import model

        while not self._stop.is_set():
            bar = await self._bars.get()
            if self.engine is None or bar.t <= self._last_closed_t:
                continue
            gap = bar.t - self._last_closed_t
            if self._last_closed_t and 60 < gap < 3 * 86400 and self.session is not None:
                try:
                    hist = await self.session.trendbars(self.symbol_id, (self._last_closed_t + 60) * 1000, bar.t * 1000,
                                                        model.M1)
                    if hist:
                        self._feed(hist, trading=False)
                except Exception as exc:
                    log.warning("gap back-fill failed: %s", exc)
            try:
                self.engine.on_bar(bar)
            except Exception:
                log.exception("engine failed on bar")
                self.engine.emit("error", "Engine error on the last bar, see logs")
            self._last_closed_t = bar.t
            self.maybe_publish_state(force=True)

    async def _housekeeping(self) -> None:
        last_news = 0.0
        last_balance = time.time()
        while not self._stop.is_set():
            now = time.time()
            if self.s.news.enabled and now - last_news > self.s.news.refresh_minutes * 60:
                last_news = now
                added = await self.news.refresh()
                if added and self.engine:
                    self.engine.emit("info", f"News calendar refreshed: {added} new events")
            if self.mode == "live" and self.broker is not None and now - last_balance > 300:
                last_balance = now
                try:
                    await self.broker.refresh_balance()
                except Exception:
                    pass
            if self.engine is not None and self.status == "running":
                try:
                    self.engine.on_clock(int(now))
                except Exception:
                    log.exception("clock rules failed")
            if self.engine is not None and self.status == "running" and self.last_quote_at and now - self.last_quote_at > 600:
                w = self.engine.w
                if w and not w.is_weekend and w.asian_start <= now <= w.flat:
                    self.engine.emit("error", "No prices for 10 minutes during the trading day")
                    self.last_quote_at = now
            self.maybe_publish_state()
            await asyncio.sleep(5)


# =============================================================================== replay
class ReplayRunner(BaseRunner):
    mode = "replay"

    def __init__(self, settings: Settings, journal: Journal, news: NewsCalendar, publish: Publish,
                 dataset: str, start: date | None, end: date | None, speed: float = 600.0):
        super().__init__(settings, journal, news, publish)
        self.dataset, self.start_d, self.end_d = dataset, start, end
        self.speed = speed  # simulated seconds per real second
        self.paused = False
        self.progress = 0.0

    async def run(self) -> None:
        from ..backtest import data as store
        from ..backtest.runner import symbol_from_costs

        try:
            self.set_status("loading", self.dataset)
            bars = await asyncio.to_thread(store.load_dataset, self.dataset, self.start_d, self.end_d)
            if len(bars) == 0:
                raise RuntimeError("no bars in the selected range")
            sim = SimBroker(symbol_from_costs(self.s), self.s.costs, self.s.backtest.initial_balance)
            self.engine = StrategyEngine(self.s, sim, self.news, emit=self.on_engine_event)
            self.broker = sim
            self.account_label = f"replay-{self.dataset}"
            self.account = {"broker": "Simulator", "login": self.dataset, "currency": "USD",
                            "balance": sim.balance(), "is_live": False}
            self.run_id = self.journal.start_run("replay", self.account_label, self.s.model_dump())
            self.set_status("running", f"replaying {self.dataset} at {self.speed:g}x")
            n = len(bars)
            for i, bar in enumerate(bars.iter_bars()):
                if self._stop.is_set():
                    break
                while self.paused and not self._stop.is_set():
                    await asyncio.sleep(0.2)
                sim.process_bar(bar)
                self.engine.on_bar(bar)
                self.progress = i / n
                self.maybe_publish_state()
                w = self.engine.w
                active = w is not None and not w.is_weekend and w.asian_start <= bar.t <= w.flat
                delay = 60 / self.speed if active else 60 / (self.speed * 50)
                if delay >= 0.001:
                    await asyncio.sleep(delay)
                elif i % 200 == 0:
                    await asyncio.sleep(0)
            self.engine.flatten_all("replay finished")
            self.set_status("finished", self.dataset)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("replay failed")
            self.set_status("error", f"{type(exc).__name__}: {exc}")

    def info(self) -> dict:
        d = super().info()
        d.update({"progress": self.progress, "speed": self.speed, "paused": self.paused})
        return d
