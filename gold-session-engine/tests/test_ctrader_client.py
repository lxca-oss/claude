import asyncio

import pytest

from gse.broker.ctrader import protocol as P
from gse.broker.ctrader.client import OpenApiError
from gse.broker.ctrader.mock_server import ACCOUNT_ID, SYMBOL_ID, MockOpenApiServer
from gse.broker.ctrader.proto import model
from gse.broker.ctrader.session import CTraderSession


def run(coro):
    return asyncio.run(coro)


async def _session(srv):
    port = await srv.start()
    s = CTraderSession("id", "secret", "token", host="127.0.0.1", port=port, use_ssl=False)
    await s.start()
    return s


def test_auth_accounts_symbol():
    async def go():
        srv = MockOpenApiServer()
        s = await _session(srv)
        accounts = await s.list_accounts()
        assert accounts[0].account_id == ACCOUNT_ID and accounts[0].is_live is False
        await s.authorize_account(ACCOUNT_ID)
        info = await s.account_info()
        assert info.balance == 100_000.0 and info.currency == "USD" and info.broker == "Mock Broker"
        light, sym = await s.find_symbol("XAUUSD")
        assert light.symbolId == SYMBOL_ID
        spec = s.symbol_spec(light, sym, reference_price=2400)
        assert spec.contract_size == 100 and spec.min_units == 1 and spec.step_units == 1
        assert spec.commission_per_unit_rt == pytest.approx(0.07)
        await s.close()
        await srv.stop()

    run(go())


def test_bad_credentials_raise():
    async def go():
        srv = MockOpenApiServer()
        port = await srv.start()
        s = CTraderSession("id", "wrong", "token", host="127.0.0.1", port=port, use_ssl=False)
        with pytest.raises(OpenApiError) as exc:
            await s.start()
        assert exc.value.code == "CH_CLIENT_AUTH_FAILURE"
        await s.close()
        await srv.stop()

    run(go())


def test_trendbars_and_ticks_history():
    async def go():
        srv = MockOpenApiServer()
        s = await _session(srv)
        await s.authorize_account(ACCOUNT_ID)
        t0 = 1_700_000_000_000 // 60_000 * 60_000
        bars = await s.trendbars(SYMBOL_ID, t0, t0 + 3 * 3_600_000, model.M1, chunk_ms=3_600_000)
        assert len(bars) == 180
        assert all(b.l <= min(b.o, b.c) and b.h >= max(b.o, b.c) for b in bars)
        assert [b.t for b in bars] == sorted(b.t for b in bars)
        ticks = await s.ticks(SYMBOL_ID, model.BID, t0, t0 + 7_200_000)  # 721 ticks -> needs paging
        assert len(ticks) == 721
        assert ticks[0][0] == t0 and ticks[-1][0] == t0 + 7_200_000
        await s.close()
        await srv.stop()

    run(go())


def test_frame_roundtrip_and_tick_decoding():
    from gse.broker.ctrader.proto import messages as M

    msg = M.ProtoOANewOrderReq(ctidTraderAccountId=1, symbolId=41, orderType=model.STOP, tradeSide=model.BUY,
                               volume=300, stopPrice=2401.5, clientOrderId="A1")
    frame = P.encode(msg, "c1")
    assert int.from_bytes(frame[:4], "big") == len(frame) - 4
    pt, back, cid = P.decode(frame[4:])
    assert pt == model.PROTO_OA_NEW_ORDER_REQ and cid == "c1" and back.stopPrice == 2401.5

    res = M.ProtoOAGetTickDataRes(ctidTraderAccountId=1, hasMore=False)
    res.tickData.add(timestamp=3000, tick=240_050_000)
    res.tickData.add(timestamp=-1000, tick=-10_000)
    res.tickData.add(timestamp=-1000, tick=5_000)
    assert P.decode_ticks(res.tickData) == [(1000, 2400.45), (2000, 2400.4), (3000, 2400.5)]
    assert P.relative_points(5.123, 2) == 512000
