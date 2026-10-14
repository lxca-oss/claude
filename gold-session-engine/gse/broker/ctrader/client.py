"""Asynchronous cTrader Open API client (Protobuf over TCP/SSL).

* request/response matching through ``clientMsgId``;
* heartbeats every 10 seconds (the server drops idle connections after ~30 s);
* client-side rate limiting under cTrader's limits (50 req/s, 5 req/s for history);
* every incoming event is handed to registered handlers.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import ssl
import time
from collections import defaultdict
from typing import Awaitable, Callable

from .proto import common, common_model, model
from . import protocol as P

log = logging.getLogger(__name__)

Handler = Callable[[int, object], None | Awaitable[None]]


class OpenApiError(Exception):
    def __init__(self, code: str, description: str = "", payload_type: int | None = None):
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description
        self.payload_type = payload_type


class _RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


class OpenApiClient:
    def __init__(self, host: str, port: int = P.PORT, use_ssl: bool = True,
                 rate_per_s: float = 40.0, history_rate_per_s: float = 4.0, heartbeat_s: float = 10.0):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.heartbeat_s = heartbeat_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._handlers: dict[int, list[Handler]] = defaultdict(list)
        self._any_handlers: list[Handler] = []
        self._tasks: list[asyncio.Task] = []
        self._rate = _RateLimiter(rate_per_s)
        self._hist_rate = _RateLimiter(history_rate_per_s)
        self._write_lock = asyncio.Lock()
        self.connected = False
        self.on_disconnect: Callable[[str], None] | None = None
        self.last_message_at = 0.0

    # ---------------------------------------------------------------- lifecycle
    async def connect(self, timeout: float = 15.0) -> None:
        ctx = ssl.create_default_context() if self.use_ssl else None
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ctx, server_hostname=self.host if ctx else None),
            timeout,
        )
        self.connected = True
        self.last_message_at = time.monotonic()
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._heartbeat_loop())]
        log.info("Connected to %s:%s", self.host, self.port)

    async def close(self) -> None:
        self.connected = False
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._fail_pending(ConnectionError("connection closed"))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ---------------------------------------------------------------- handlers
    def on(self, payload_type: int, handler: Handler) -> None:
        self._handlers[payload_type].append(handler)

    def on_any(self, handler: Handler) -> None:
        self._any_handlers.append(handler)

    # ---------------------------------------------------------------- sending
    async def _write(self, data: bytes) -> None:
        if not self.connected or self._writer is None:
            raise ConnectionError("not connected")
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def send(self, msg, client_msg_id: str | None = None) -> None:
        limiter = self._hist_rate if msg.payloadType in P.HISTORICAL_TYPES else self._rate
        await limiter.wait()
        await self._write(P.encode(msg, client_msg_id))

    async def request(self, msg, timeout: float = 20.0):
        """Send ``msg`` and wait for the message carrying the same clientMsgId."""
        cid = f"gse{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            await self.send(msg, cid)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(cid, None)

    # ---------------------------------------------------------------- receiving
    async def _read_loop(self) -> None:
        reason = "connection closed by server"
        try:
            while True:
                header = await self._reader.readexactly(4)
                size = int.from_bytes(header, "big")
                body = await self._reader.readexactly(size)
                self.last_message_at = time.monotonic()
                try:
                    pt, msg, cid = P.decode(body)
                except Exception:
                    log.exception("Could not decode a message")
                    continue
                try:
                    await self._dispatch(pt, msg, cid)
                except Exception:
                    log.exception("Dispatch of %s failed", P.message_name(pt))
        except asyncio.CancelledError:
            return
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        self.connected = False
        self._fail_pending(ConnectionError(reason))
        log.warning("Disconnected: %s", reason)
        if self.on_disconnect:
            self.on_disconnect(reason)

    async def _dispatch(self, pt: int, msg, cid: str | None) -> None:
        if pt == common_model.HEARTBEAT_EVENT:
            return
        if cid and cid in self._pending:
            fut = self._pending[cid]
            if not fut.done():
                if pt in P.ERROR_TYPES or pt == model.PROTO_OA_ORDER_ERROR_EVENT:
                    fut.set_exception(OpenApiError(getattr(msg, "errorCode", "ERROR"),
                                                   getattr(msg, "description", ""), pt))
                else:
                    fut.set_result(msg)
        for h in [*self._handlers.get(pt, ()), *self._any_handlers]:
            try:
                res = h(pt, msg)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("Handler for %s failed", P.message_name(pt))

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_s)
                try:
                    await self._write(P.encode(common.ProtoHeartbeatEvent()))
                except Exception:
                    return
        except asyncio.CancelledError:
            return

