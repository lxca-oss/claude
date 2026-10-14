import asyncio
import time

from gse.broker.ctrader import protocol as P
from gse.broker.ctrader.mock_server import ACCOUNT_ID, SYMBOL_ID, MockOpenApiServer
from gse.broker.ctrader.proto import messages as M
from gse.config import Settings, save_secrets
from gse.journal import Journal
from gse.live.runner import LiveRunner
from gse.news import NewsCalendar


def spot(bid, ask, ts_ms):
    return M.ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT_ID, symbolId=SYMBOL_ID, bid=round(bid * P.PRICE_SCALE),
                              ask=round(ask * P.PRICE_SCALE), timestamp=ts_ms)


async def start_runner(mode):
    srv = MockOpenApiServer()
    port = await srv.start()
    save_secrets({"client_id": "id", "client_secret": "secret", "access_token": "token"})
    s = Settings()
    s.news.enabled = False
    s.news.refresh_minutes = 100000
    published = []
    runner = LiveRunner(s, Journal(), NewsCalendar(s.news), lambda k, p: published.append(k),
                        account_id=ACCOUNT_ID, is_live=False, mode=mode, host="127.0.0.1", port=port, use_ssl=False)
    task = asyncio.create_task(runner.run())
    for _ in range(400):
        await asyncio.sleep(0.05)
        if runner.status == "running":
            break
    return srv, runner, task, published


def test_paper_runner_connects_warms_up_and_builds_bars():
    async def go():
        srv, runner, task, published = await start_runner("paper")
        assert runner.status == "running", (runner.status, runner.detail)
        eng = runner.engine
        assert len(eng.m5) > 100 and eng.atr5.value is not None  # warmed up from 4 days of M1 history
        assert runner.account["broker"] == "Mock Broker" and runner.account["symbol"]["commission_per_oz_rt"] == 0.07
        last = runner._last_closed_t
        t0 = (last + 120) * 1000
        runner._on_spot(None, spot(2401.0, 2401.3, t0 + 1000))
        runner._on_spot(None, spot(2402.0, 2402.2, t0 + 20_000))
        runner._on_spot(None, spot(2400.5, 2400.8, t0 + 61_000))  # next minute closes the first
        await asyncio.sleep(0.2)
        assert runner._last_closed_t == t0 // 1000
        bar = eng.last_bar
        assert (bar.o, bar.h, bar.l, bar.c) == (2401.0, 2402.0, 2401.0, 2402.0)
        assert abs(bar.s - 0.25) < 1e-9
        assert eng.bid == 2400.5
        assert "state" in published
        await runner.stop()
        task.cancel()
        await srv.stop()

    asyncio.run(go())


def test_live_runner_reconnects_after_disconnect():
    async def go():
        srv, runner, task, published = await start_runner("live")
        assert runner.status == "running"
        for w in list(srv.writers):
            w.close()  # server drops the connection
        for _ in range(100):
            await asyncio.sleep(0.05)
            if runner.status in ("reconnecting", "connecting"):
                break
        for _ in range(200):
            await asyncio.sleep(0.05)
            if runner.status == "running":
                break
        assert runner.status == "running"
        assert srv.received.count("ProtoOAApplicationAuthReq") >= 2
        await runner.stop()
        task.cancel()
        await srv.stop()

    asyncio.run(go())
