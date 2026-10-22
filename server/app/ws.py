"""The /ws endpoint: handshake limits, the read loop and an outgoing queue per connection.

The client IP is used only here, at the handshake, to derive the limits key. It is never
logged, stored on the connection or put into an exception message.
"""

import asyncio
import ipaddress
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from app.clock import Clock
from app.limits import UNKNOWN, Limits, TokenBucket, ip_key
from app.logs import log_exception, logger
from app.protocol import (
    CLOSE_RATE_LIMIT,
    CLOSE_SLOW_READER,
    BadRequestError,
    ServerError,
    dump_server_message,
    parse_client_message,
)
from app.rooms import Hub

OUTBOX_MAX_MESSAGES: Final = 256
OUTBOX_MAX_BYTES: Final = 1_048_576
CLOSE_WAIT_SECONDS: Final = 1.0
SHARED_KEY_WARNING: Final = "X-Forwarded-For отсутствует, лимиты общие"

_BAD_REQUEST: Final = dump_server_message(ServerError(code="bad-request"))
_LIMIT_CODES: Final = frozenset({CLOSE_RATE_LIMIT, CLOSE_SLOW_READER})
# A send to a client that is already gone; not worth an error line.
_GONE: Final = (WebSocketDisconnect, WebSocketDisconnected, OSError)


class Socket(Protocol):
    """The part of Starlette's WebSocket the connection uses."""

    async def accept(self) -> None: ...

    async def receive(self) -> Mapping[str, Any]: ...

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


@dataclass(eq=False)
class WsState:
    """Per-application state shared by all connections; lives in app.state.ws."""

    hub: Hub
    limits: Limits
    clock: Clock
    closed_by_limits: int = 0  # refused handshakes and closes with 1008 or 4001
    warned_shared_key: bool = False


def _is_shared_key(host: str | None, key: str) -> bool:
    """No usable client address: every client behind it shares one limits key."""
    if key == UNKNOWN or host is None:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


class Connection:
    """Implements rooms.Conn over a WebSocket: an outgoing queue drained by a writer task,
    and message and byte buckets for incoming frames."""

    def __init__(self, websocket: Socket, key: str, state: WsState) -> None:
        self._ws = websocket
        self.key = key
        self._state = state
        config = state.limits.config
        now = state.clock.now()
        self._messages = TokenBucket(config.msg_burst, config.msg_rate, now)
        self._bytes = TokenBucket(config.bytes_burst, config.bytes_rate, now)
        # The head stays queued while the writer sends it, so it counts against the limits.
        self._outbox: deque[tuple[str, int]] = deque()
        self._outbox_bytes = 0
        self._open = True
        self._close_code: int | None = None
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._writer: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"Connection(open={self._open}, queued={len(self._outbox)})"

    @property
    def closed(self) -> bool:
        return not self._open

    def send(self, text: str) -> None:
        if not self._open:
            return
        size = len(text.encode())
        if (
            len(self._outbox) >= OUTBOX_MAX_MESSAGES
            or self._outbox_bytes + size > OUTBOX_MAX_BYTES
            or not self._state.limits.reserve(size)
        ):
            self.close(CLOSE_SLOW_READER)
            return
        self._outbox.append((text, size))
        self._outbox_bytes += size
        self._wake.set()

    def close(self, code: int) -> None:
        """Stop reading and sending; the writer sends the close frame."""
        if not self._open:
            return
        self._close_code = code
        if code in _LIMIT_CODES:
            self._state.closed_by_limits += 1
        self._shut()
        self._wake.set()

    def stop(self) -> None:
        """Abandon the connection without a close frame. Idempotent."""
        self._shut()
        if self._writer is not None:
            self._writer.cancel()

    def _shut(self) -> None:
        self._open = False
        self._state.limits.free(self._outbox_bytes)
        self._outbox.clear()
        self._outbox_bytes = 0
        self._stopped.set()

    async def run(self) -> None:
        """Read until the client leaves or the connection is closed.

        After close(code) the close frame gets at most CLOSE_WAIT_SECONDS.
        """
        self._writer = asyncio.create_task(self._write())
        reader = asyncio.create_task(self._read())
        stopped = asyncio.create_task(self._stopped.wait())
        try:
            await asyncio.wait((reader, stopped), return_when=asyncio.FIRST_COMPLETED)
            if self._close_code is not None:
                await asyncio.wait((self._writer,), timeout=CLOSE_WAIT_SECONDS)
        finally:
            self.stop()
            reader.cancel()
            stopped.cancel()
        if reader.done() and not reader.cancelled():
            exc = reader.exception()
            if exc is not None:
                raise exc

    async def _read(self) -> None:
        hub = self._state.hub
        while True:
            message = await self._ws.receive()
            if message["type"] != "websocket.receive" or not self._open:
                return
            text: str | None = message.get("text")
            data: bytes | None = message.get("bytes")
            if text is not None:
                size = len(text.encode(errors="surrogatepass"))
            else:
                size = len(data or b"")
            if not self._admit(size):
                self.close(CLOSE_RATE_LIMIT)
                return
            if text is None:
                self.send(_BAD_REQUEST)
                continue
            try:
                parsed = parse_client_message(text)
            except BadRequestError:
                self.send(_BAD_REQUEST)
                continue
            hub.handle(self, parsed)

    def _admit(self, size: int) -> bool:
        now = self._state.clock.now()
        return (
            self._messages.take(1, now)
            and self._bytes.take(size, now)
            and self._state.limits.take_key_bytes(self.key, size)
        )

    async def _write(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._open and self._outbox:
                    text, size = self._outbox[0]
                    await self._ws.send_text(text)
                    if not self._open:
                        break  # close() already dropped the queue with this message
                    self._outbox.popleft()
                    self._outbox_bytes -= size
                    self._state.limits.free(size)
                if not self._open:
                    if self._close_code is not None:
                        await self._ws.close(self._close_code)
                    return
        except _GONE:
            self._shut()
        except Exception as exc:
            log_exception(logger, "ws writer", exc)
            self._shut()


async def serve(websocket: Socket, host: str | None, state: WsState) -> None:
    """Run one client connection. Never raises: errors are logged by type name only."""
    key = ip_key(host)
    if not state.warned_shared_key and _is_shared_key(host, key):
        state.warned_shared_key = True
        logger.warning(SHARED_KEY_WARNING)
    if not state.limits.try_open(key):
        state.closed_by_limits += 1
        try:
            await websocket.close()  # before accept: uvicorn answers 403
        except Exception as exc:
            log_exception(logger, "ws refuse", exc)
        return
    conn = Connection(websocket, key, state)
    try:
        await websocket.accept()
        await conn.run()
    except Exception as exc:
        log_exception(logger, "ws handler", exc)
    finally:
        # Synchronous on purpose: a cancelled handler still releases its slot exactly once.
        try:
            state.hub.disconnected(conn)
        except Exception as exc:
            log_exception(logger, "ws disconnect", exc)
        state.limits.connection_closed()
        conn.stop()


router: Final = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    state: WsState = websocket.app.state.ws
    await serve(websocket, websocket.client.host if websocket.client else None, state)
