import asyncio
import time

import pytest

from gse.broker.ctrader.broker import CTraderBroker
from gse.broker.ctrader.mock_server import ACCOUNT_ID, SYMBOL_ID, MockOpenApiServer
from gse.broker.ctrader.session import CTraderSession
from gse.models import Order


class Recorder:
    def __init__(self):
        self.events = []

    def on_order_filled(self, order, position, ts):
        self.events.append(("fill", order.ref, position.entry, position.units))

    def on_order_cancelled(self, order, reason, ts):
        self.events.append(("cancel", order.ref, reason))

    def on_order_rejected(self, order, reason, ts):
        self.events.append(("reject", order.ref, reason))

    def on_position_closed(self, position, trade):
        self.events.append(("close", trade.ref, trade.exit_reason, round(trade.pnl, 2), round(trade.r, 3)))

    def on_position_event(self, position, kind, info, ts):
        self.events.append((kind, position.ref))


def order(ref, side, kind, entry, sl, tp, units=10, **kw):
    return Order(ref=ref, module=ref[0], side=side, kind=kind, units=units, entry=entry, sl=sl, tp=tp,
                 risk_money=units * abs(entry - sl), r_dist=abs(entry - sl), created_ts=int(time.time()), **kw)


async def setup():
    srv = MockOpenApiServer(start_price=2400.0)
    port = await srv.start()
    s = CTraderSession("id", "secret", "token", host="127.0.0.1", port=port, use_ssl=False)
    await s.start()
    await s.authorize_account(ACCOUNT_ID)
    light, sym = await s.find_symbol("XAUUSD")
    spec = s.symbol_spec(light, sym, reference_price=2400)
    b = CTraderBroker(s, SYMBOL_ID, spec, "GSE", balance=100_000.0, money_digits=2)
    rec = Recorder()
    b.set_listener(rec)
    await s.subscribe_spots([SYMBOL_ID])
    from gse.broker.ctrader import protocol as P
    from gse.broker.ctrader.proto import model

    def on_spot(pt, ev):
        b.on_quote(int(time.time()), ev.bid / P.PRICE_SCALE if ev.HasField("bid") else None,
                   ev.ask / P.PRICE_SCALE if ev.HasField("ask") else None)

    s.client.on(model.PROTO_OA_SPOT_EVENT, on_spot)
    await srv.push_quote(2400.0)
    await asyncio.sleep(0.05)
    return srv, s, b, rec


async def settle():
    await asyncio.sleep(0.1)


def test_oco_stop_orders_fill_and_target():
    async def go():
        srv, s, b, rec = await setup()
        b.place(order("A1", 1, "stop", 2405.0, 2395.0, 2425.0, oco_group="g", be_trigger=2420.0))
        b.place(order("A2", -1, "stop", 2395.0, 2405.0, 2375.0, oco_group="g"))
        await settle()
        assert all(o.broker_id for o in b.orders.values())
        await srv.push_quote(2405.0)  # ask 2405.25 triggers the buy stop
        await settle()
        assert ("fill", "A1", 2405.25, 10) in rec.events
        assert ("cancel", "A2", "OCO: other side filled") in rec.events
        assert "A1" in b.positions and not b.orders
        await srv.push_quote(2420.5)  # break-even trigger
        await settle()
        assert b.positions["A1"].be_done and b.positions["A1"].sl == 2405.25
        await srv.push_quote(2426.0)  # take profit
        await settle()
        close = [e for e in rec.events if e[0] == "close"][0]
        assert close[1] == "A1" and close[2] == "tp"
        # gross (2426 - 2405.25) * 10 = 207.5, commission 0.35 x2 = 0.70
        assert close[3] == pytest.approx(207.5 - 0.70, abs=0.02)
        assert b.balance() == pytest.approx(100_000 + 207.5 - 0.70, abs=0.02)
        await s.close()
        await srv.stop()

    asyncio.run(go())


def test_market_order_partial_breakeven_and_stop():
    async def go():
        srv, s, b, rec = await setup()
        o = order("B1", -1, "market", 2400.0, 2406.0, 2388.0, units=20, partial_price=2394.0,
                  partial_fraction=0.5, be_trigger=2394.0)
        b.place(o)
        await settle()
        p = b.positions["B1"]
        assert p.entry == 2400.0 and p.units == 20
        await settle()
        assert p.sl == 2406.0 and p.tp == 2388.0  # amended to the planned absolute levels
        await srv.push_quote(2393.5, 2393.75)  # ask below 2394: partial + break-even
        await settle()
        assert p.partial_done and p.units == 10 and p.be_done and p.sl == 2400.0
        await srv.push_quote(2400.0, 2400.25)  # back up: stopped at break-even
        await settle()
        close = [e for e in rec.events if e[0] == "close"][0]
        assert close[2] == "breakeven"
        assert not b.positions
        await s.close()
        await srv.stop()

    asyncio.run(go())


def test_cancel_close_and_reject():
    async def go():
        srv, s, b, rec = await setup()
        b.place(order("C1", 1, "stop", 2410.0, 2404.0, 2422.0))
        await settle()
        b.cancel("C1", "news: CPI")
        await settle()
        assert ("cancel", "C1", "news: CPI") in rec.events
        b.place(order("C2", 1, "market", 2400.25, 2394.0, 2412.0))
        await settle()
        b.close("C2", "session close 11:30 NY")
        await settle()
        assert [e for e in rec.events if e[0] == "close"][0][2] == "session close 11:30 NY"
        b.place(order("C3", 1, "market", 2400.25, 2394.0, 2412.0, units=0.5))  # bad volume
        await settle()
        assert any(e[0] == "reject" and e[1] == "C3" for e in rec.events)
        await s.close()
        await srv.stop()

    asyncio.run(go())


def test_restore_adopts_own_positions_and_cancels_stale_orders():
    async def go():
        srv, s, b, rec = await setup()
        b.place(order("A9", 1, "market", 2400.25, 2392.0, 2420.0))
        b.place(order("C9", 1, "stop", 2410.0, 2404.0, 2422.0))
        await settle()
        # a fresh broker (bot restart) finds the position and removes the stale stop order
        b2 = CTraderBroker(s, SYMBOL_ID, b.symbol, "GSE", balance=b.balance(), money_digits=2,
                           trade_meta=lambda ref: {"module": "A", "r_dist": 8.25, "risk_money": 82.5} if ref == "A9" else None)
        restored = await b2.restore()
        assert [p.ref for p in restored] == ["A9"] and restored[0].module == "A" and restored[0].r_dist == 8.25
        assert not srv.orders
        await s.close()
        await srv.stop()

    asyncio.run(go())
