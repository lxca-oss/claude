"""High-level cTrader Open API session: application auth, accounts, symbols and history."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ...bars import Bar
from ...models import SymbolSpec
from . import protocol as P
from .client import OpenApiClient, OpenApiError
from .proto import messages as M
from .proto import model

log = logging.getLogger(__name__)


@dataclass
class AccountInfo:
    account_id: int
    is_live: bool
    login: int | None
    broker: str = ""
    balance: float | None = None
    currency: str = ""
    leverage: float | None = None
    money_digits: int = 2
    account_type: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class CTraderSession:
    """One authenticated connection to a cTrader Open API host."""

    def __init__(self, client_id: str, client_secret: str, access_token: str, live: bool = False,
                 host: str | None = None, port: int = P.PORT, use_ssl: bool = True):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.live = live
        self.client = OpenApiClient(host or (P.LIVE_HOST if live else P.DEMO_HOST), port, use_ssl)
        self.account_id: int | None = None
        self.assets: dict[int, str] = {}
        self.app_authorized = False

    async def start(self) -> None:
        await self.client.connect()
        await self.client.request(M.ProtoOAApplicationAuthReq(clientId=self.client_id, clientSecret=self.client_secret))
        self.app_authorized = True

    async def close(self) -> None:
        if self.account_id is not None and self.client.connected:
            try:
                await self.client.request(M.ProtoOAAccountLogoutReq(ctidTraderAccountId=self.account_id), timeout=5)
            except Exception:
                pass
        await self.client.close()

    # ---------------------------------------------------------------- accounts
    async def list_accounts(self) -> list[AccountInfo]:
        res = await self.client.request(M.ProtoOAGetAccountListByAccessTokenReq(accessToken=self.access_token))
        return [
            AccountInfo(account_id=int(a.ctidTraderAccountId), is_live=bool(a.isLive),
                        login=int(a.traderLogin) if a.HasField("traderLogin") else None)
            for a in res.ctidTraderAccount
        ]

    async def authorize_account(self, account_id: int) -> None:
        await self.client.request(M.ProtoOAAccountAuthReq(ctidTraderAccountId=account_id, accessToken=self.access_token))
        self.account_id = account_id

    async def refresh_token_via_socket(self, refresh_token: str) -> dict:
        res = await self.client.request(M.ProtoOARefreshTokenReq(refreshToken=refresh_token))
        return {"access_token": res.accessToken, "refresh_token": res.refreshToken, "expires_in": res.expiresIn}

    async def trader(self):
        res = await self.client.request(M.ProtoOATraderReq(ctidTraderAccountId=self.account_id))
        return res.trader

    async def load_assets(self) -> dict[int, str]:
        res = await self.client.request(M.ProtoOAAssetListReq(ctidTraderAccountId=self.account_id))
        self.assets = {int(a.assetId): a.name for a in res.asset}
        return self.assets

    async def account_info(self) -> AccountInfo:
        t = await self.trader()
        if not self.assets:
            await self.load_assets()
        md = t.moneyDigits if t.HasField("moneyDigits") else 2
        return AccountInfo(
            account_id=self.account_id, is_live=self.live,
            login=int(t.traderLogin) if t.HasField("traderLogin") else None,
            broker=t.brokerName, balance=P.money(t.balance, md), currency=self.assets.get(t.depositAssetId, "?"),
            leverage=t.leverageInCents / 100 if t.HasField("leverageInCents") else None, money_digits=md,
            account_type=model.ProtoOAAccountType.Name(t.accountType) if t.HasField("accountType") else "",
        )

    # ---------------------------------------------------------------- symbols
    async def find_symbol(self, name: str):
        res = await self.client.request(M.ProtoOASymbolsListReq(ctidTraderAccountId=self.account_id))
        wanted = name.upper().replace("/", "")
        light = None
        for s in res.symbol:
            if s.symbolName.upper().replace("/", "") == wanted:
                light = s
                break
        if light is None:  # brokers sometimes add suffixes (XAUUSD.r, XAUUSDm, ...)
            for s in res.symbol:
                if s.symbolName.upper().replace("/", "").startswith(wanted):
                    light = s
                    break
        if light is None:
            raise OpenApiError("SYMBOL_NOT_FOUND", f"{name} is not available on this account")
        full = await self.client.request(
            M.ProtoOASymbolByIdReq(ctidTraderAccountId=self.account_id, symbolId=[light.symbolId]))
        return light, full.symbol[0]

    @staticmethod
    def symbol_spec(light, sym, commission_override: float | None = None,
                    reference_price: float | None = None) -> SymbolSpec:
        lot_units = P.volume_to_units(sym.lotSize) if sym.HasField("lotSize") else 100.0
        comm = commission_override
        if comm is None and reference_price:
            fn = commission_model(sym, lot_units)
            comm = fn(reference_price) if fn else None
        return SymbolSpec(
            name=light.symbolName, digits=sym.digits, contract_size=lot_units,
            min_units=P.volume_to_units(sym.minVolume) if sym.HasField("minVolume") else 1.0,
            step_units=P.volume_to_units(sym.stepVolume) if sym.HasField("stepVolume") else 1.0,
            max_units=P.volume_to_units(sym.maxVolume) if sym.HasField("maxVolume") else 1e9,
            commission_per_unit_rt=comm if comm is not None else 0.07,
        )

    async def conversion_chain(self, from_asset: int, to_asset: int) -> list:
        if from_asset == to_asset:
            return []
        res = await self.client.request(M.ProtoOASymbolsForConversionReq(
            ctidTraderAccountId=self.account_id, firstAssetId=from_asset, lastAssetId=to_asset))
        return list(res.symbol)

    # ---------------------------------------------------------------- subscriptions
    async def subscribe_spots(self, symbol_ids: list[int]) -> None:
        try:
            await self.client.request(M.ProtoOASubscribeSpotsReq(
                ctidTraderAccountId=self.account_id, symbolId=symbol_ids, subscribeToSpotTimestamp=True))
        except OpenApiError as exc:
            if exc.code != "ALREADY_SUBSCRIBED":
                raise

    async def subscribe_live_bars(self, symbol_id: int, period: int = model.M1) -> None:
        try:
            await self.client.request(M.ProtoOASubscribeLiveTrendbarReq(
                ctidTraderAccountId=self.account_id, symbolId=symbol_id, period=period))
        except OpenApiError as exc:
            if exc.code != "ALREADY_SUBSCRIBED":
                raise

    async def reconcile(self):
        return await self.client.request(M.ProtoOAReconcileReq(ctidTraderAccountId=self.account_id))

    # ---------------------------------------------------------------- history
    async def trendbars(self, symbol_id: int, from_ms: int, to_ms: int, period: int = model.M1,
                        chunk_ms: int = 2 * 86_400_000) -> list[Bar]:
        """Closed bars in [from_ms, to_ms), fetched in chunks (the server caps each response)."""
        out: dict[int, Bar] = {}
        start = from_ms
        secs = P.PERIOD_SECONDS.get(period, 60)
        while start < to_ms:
            end = min(start + chunk_ms, to_ms)
            res = await self.client.request(M.ProtoOAGetTrendbarsReq(
                ctidTraderAccountId=self.account_id, symbolId=symbol_id, period=period,
                fromTimestamp=start, toTimestamp=end), timeout=60)
            for tb in res.trendbar:
                b = P.decode_trendbar(tb, secs)
                out[b.t] = b
            start = end
        return [out[k] for k in sorted(out)]

    async def ticks(self, symbol_id: int, quote: int, from_ms: int, to_ms: int) -> list[tuple[int, float]]:
        """All ticks of one side (model.BID / model.ASK) in [from_ms, to_ms]."""
        collected: dict[int, float] = {}
        hi = to_ms
        lo = from_ms
        for _ in range(10_000):
            res = await self.client.request(M.ProtoOAGetTickDataReq(
                ctidTraderAccountId=self.account_id, symbolId=symbol_id, type=quote,
                fromTimestamp=lo, toTimestamp=hi), timeout=60)
            ticks = P.decode_ticks(res.tickData)
            for ts, px in ticks:
                if from_ms <= ts <= to_ms:
                    collected[ts] = px
            if not res.hasMore or not ticks:
                break
            oldest, newest = ticks[0][0], ticks[-1][0]
            # the server pages from the newest tick backwards; keep paging towards from_ms
            if oldest - 1 < hi and oldest > lo:
                hi = oldest - 1
            elif newest + 1 > lo and newest < hi:
                lo = newest + 1
            else:
                break
            await asyncio.sleep(0)
        return sorted(collected.items())

    async def deals(self, from_ms: int, to_ms: int) -> list:
        out = []
        start = from_ms
        while start < to_ms:
            end = min(start + 7 * 86_400_000, to_ms)
            res = await self.client.request(M.ProtoOADealListReq(
                ctidTraderAccountId=self.account_id, fromTimestamp=start, toTimestamp=end))
            out.extend(res.deal)
            start = end
        return out


def commission_model(sym, lot_units: float):
    """Round-turn commission per ounce (quote currency) as a function of price, or None.

    cTrader charges per side. ``preciseTradingCommissionRate`` is the rate scaled by 1e8
    (1e5 for percentage of value). The terminal shows the resulting value so it can be
    checked against the broker's contract specs and overridden in settings.
    """
    if not sym.HasField("commissionType"):
        return None
    if sym.HasField("preciseTradingCommissionRate"):
        rate = sym.preciseTradingCommissionRate
        scale = 1e5 if sym.commissionType == model.PERCENTAGE_OF_VALUE else 1e8
    elif sym.HasField("commission"):
        rate, scale = sym.commission, 1e2
    else:
        return None
    value = rate / scale
    ct = sym.commissionType
    if ct in (model.USD_PER_LOT, model.QUOTE_CCY_PER_LOT):
        return lambda price: 2 * value / lot_units
    if ct == model.USD_PER_MILLION_USD:
        return lambda price: 2 * value / 1_000_000 * price
    if ct == model.PERCENTAGE_OF_VALUE:
        return lambda price: 2 * value / 100 * price
    return None
