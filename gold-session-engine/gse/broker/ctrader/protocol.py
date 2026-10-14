"""cTrader Open API wire format and value conversions.

Every message on the TCP/SSL socket is a 4-byte big-endian length followed by a
serialized ``ProtoMessage`` envelope {payloadType, payload, clientMsgId}. Prices in spot
events, trendbars and ticks are integers in 1/100000 of a unit; volumes are in 1/100 of
a unit (for gold, 100 = 1 oz); money fields are integers scaled by ``moneyDigits``.
"""

from __future__ import annotations

import inspect

from ...bars import Bar
from .proto import common, common_model, messages, model

PRICE_SCALE = 100_000
VOLUME_SCALE = 100

DEMO_HOST = "demo.ctraderapi.com"
LIVE_HOST = "live.ctraderapi.com"
PORT = 5035

HISTORICAL_TYPES = {
    model.PROTO_OA_GET_TRENDBARS_REQ,
    model.PROTO_OA_GET_TICKDATA_REQ,
    model.PROTO_OA_DEAL_LIST_REQ,
    model.PROTO_OA_ORDER_LIST_REQ,
    model.PROTO_OA_CASH_FLOW_HISTORY_LIST_REQ,
}
ERROR_TYPES = {model.PROTO_OA_ERROR_RES, common_model.ERROR_RES}


def _build_registry() -> dict[int, type]:
    reg: dict[int, type] = {}
    for mod in (common, messages):
        for name, cls in inspect.getmembers(mod, inspect.isclass):
            if not name.startswith("Proto") or name == "ProtoMessage":
                continue
            try:
                pt = cls().payloadType
            except AttributeError:
                continue
            if pt:
                reg[int(pt)] = cls
    return reg


REGISTRY = _build_registry()


def message_name(payload_type: int) -> str:
    cls = REGISTRY.get(payload_type)
    return cls.__name__ if cls else f"payload {payload_type}"


def encode(msg, client_msg_id: str | None = None) -> bytes:
    env = common.ProtoMessage(payloadType=msg.payloadType, payload=msg.SerializeToString())
    if client_msg_id:
        env.clientMsgId = client_msg_id
    data = env.SerializeToString()
    return len(data).to_bytes(4, "big") + data


def decode(data: bytes):
    """Parse one envelope (without the length prefix) -> (payload_type, message, client_msg_id)."""
    env = common.ProtoMessage()
    env.ParseFromString(data)
    cls = REGISTRY.get(env.payloadType)
    msg = None
    if cls is not None:
        msg = cls()
        msg.ParseFromString(env.payload)
    return env.payloadType, msg, (env.clientMsgId if env.HasField("clientMsgId") else None)


# ---------------------------------------------------------------------------- conversions
def price(x: int) -> float:
    return x / PRICE_SCALE


def money(x: int, money_digits: int) -> float:
    return x / (10 ** money_digits)


def units_to_volume(units: float) -> int:
    return int(round(units * VOLUME_SCALE))


def volume_to_units(volume: int) -> float:
    return volume / VOLUME_SCALE


def relative_points(distance: float, digits: int) -> int:
    """A price distance as an Open API relative value (1/100000 units) on the symbol's tick grid."""
    step = 10 ** (5 - digits)
    return max(step, int(round(distance * PRICE_SCALE / step)) * step)


def decode_trendbar(tb, period_seconds: int = 60) -> Bar:
    low = tb.low
    o = (low + tb.deltaOpen) / PRICE_SCALE
    h = (low + tb.deltaHigh) / PRICE_SCALE
    c = (low + tb.deltaClose) / PRICE_SCALE
    return Bar(t=tb.utcTimestampInMinutes * 60, o=o, h=h, l=low / PRICE_SCALE, c=c, v=float(tb.volume), s=0.0,
               tf=period_seconds)


def decode_ticks(tick_data) -> list[tuple[int, float]]:
    """Tick data arrives delta-encoded: the first entry is absolute, the rest are differences.

    Returns (timestamp_ms, price) sorted oldest first.
    """
    out = []
    ts = px = 0
    for i, td in enumerate(tick_data):
        if i == 0:
            ts, px = td.timestamp, td.tick
        else:
            ts += td.timestamp
            px += td.tick
        out.append((ts, px / PRICE_SCALE))
    out.sort(key=lambda x: x[0])
    return out


PERIOD_SECONDS = {
    model.M1: 60, model.M5: 300, model.M15: 900, model.M30: 1800, model.H1: 3600, model.H4: 14400, model.D1: 86400,
}
