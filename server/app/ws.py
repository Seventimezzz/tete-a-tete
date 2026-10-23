"""The /ws endpoint: handshake limits, the read loop and an outgoing queue per connection.

The client IP is used only here, at the handshake, to derive the limits key. It is never
logged, stored on the connection or put into an exception message.
"""

import asyncio
import ipaddress
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from app.clock import Clock
from app.limits import UNKNOWN, Limits, OutboxLedger, TokenBucket, ip_key
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

# Above what the four other members of a room can send at once (4 x 200 messages of burst).
OUTBOX_MAX_MESSAGES: Final = 1024
OUTBOX_MAX_BYTES: Final = 2_097_152
# All outgoing queues of one IP key: two full queues. Over it, the key's largest queue is closed.
KEY_OUTBOX_MAX_BYTES: Final = 4_194_304
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
    warned_no_abort: bool = False
    close_wait: float | None = None  # integration tests only: replaces CLOSE_WAIT_SECONDS

    @property
    def close_window(self) -> float:
        """How long a closing handshake may take before the transport is aborted."""
        return CLOSE_WAIT_SECONDS if self.close_wait is None else self.close_wait

    outboxes: OutboxLedger["Connection"] = field(default_factory=OutboxLedger)


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


type Abort = Callable[[], None]

# ASGI scope extension set by app.wsproto.TeteWebSocketProtocol: {"abort": <transport.abort>}.
ABORT_EXTENSION: Final = "tete.transport.abort"
NO_ABORT_WARNING: Final = (
    "обрыв транспорта недоступен: uvicorn запущен без --ws app.wsproto:TeteWebSocketProtocol"
)


def transport_abort(scope: Mapping[str, Any]) -> Abort | None:
    """The abort of the TCP transport behind a WebSocket, from its ASGI scope, or None.

    ASGI has no way to drop a connection: after `websocket.close` uvicorn closes the transport
    gracefully, and asyncio then waits for the send buffer to drain, without a time limit. A
    client that does not read keeps such a socket open forever. Our uvicorn protocol puts the
    transport's abort into the scope; under another server or the test client it is not
    there, and the connection is closed the ASGI way only (ws_endpoint warns once). The scope
    passes unchanged through uvicorn's middlewares, so the hook works at any log level: the
    earlier lookup through the `receive` callable broke under --log-level trace, where
    uvicorn wraps `receive` to log messages.
    """
    extensions = scope.get("extensions")
    hook = extensions.get(ABORT_EXTENSION) if isinstance(extensions, Mapping) else None
    abort = hook.get("abort") if isinstance(hook, Mapping) else None
    return abort if callable(abort) else None


class Connection:
    """Implements rooms.Conn over a WebSocket: an outgoing queue drained by a writer task,
    and message and byte buckets for incoming frames."""

    def __init__(
        self, websocket: Socket, key: str, state: WsState, *, abort: Abort | None = None
    ) -> None:
        self._ws = websocket
        self.key = key
        self._state = state
        self._abort = abort
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
        self._detached = False

    def __repr__(self) -> str:
        return f"Connection(open={self._open}, queued={len(self._outbox)})"

    @property
    def closed(self) -> bool:
        return not self._open

    def send(self, text: str) -> None:
        if not self._open:
            return
        size = len(text.encode())
        if len(self._outbox) >= OUTBOX_MAX_MESSAGES or self._outbox_bytes + size > OUTBOX_MAX_BYTES:
            self.close(CLOSE_SLOW_READER)
            return
        if not self._make_room(size):
            return
        self._outbox.append((text, size))
        self._outbox_bytes += size
        self._state.outboxes.change(self.key, self, size)
        self._wake.set()

    def _make_room(self, size: int) -> bool:
        """Fit `size` more bytes into the key's and the global queue budgets and reserve them.

        Over a budget, the connection holding the most queued bytes is the one not reading: it
        is closed with 4001 if that frees enough, and this one keeps the message. Otherwise
        (this one holds the most, or the bytes sit in wait queues, which are nobody's outbox)
        this connection is closed, as before.
        """
        outboxes, limits = self._state.outboxes, self._state.limits
        over_key = outboxes.key_bytes(self.key) + size - KEY_OUTBOX_MAX_BYTES
        if over_key > 0 and not self._close_hoarder(outboxes.largest(self.key), over_key):
            return False
        over = limits.queued_bytes + size - limits.config.max_queued_bytes
        if over > 0 and not self._close_hoarder(outboxes.largest(), over):
            return False
        if not limits.reserve(size):
            self.close(CLOSE_SLOW_READER)
            return False
        return True

    def _close_hoarder(self, largest: "tuple[Connection, int] | None", needed: int) -> bool:
        """Close the holder of the largest queue if it is another connection and closing it
        frees `needed` bytes; else close this one. True if this one stays open."""
        if largest is None or largest[0] is self or largest[1] < needed:
            self.close(CLOSE_SLOW_READER)
            return False
        largest[0].close(CLOSE_SLOW_READER)
        return True

    def close(self, code: int) -> None:
        """Stop reading and sending; the writer sends the close frame."""
        if not self._open:
            return
        self._close_code = code
        if code in _LIMIT_CODES:
            self._state.closed_by_limits += 1
        self._shut()
        self._wake.set()
        # The server has given up on the connection: its place waits from now on, not from
        # the end of the handler, which may wait for the close frame.
        self.detach()

    def detach(self) -> None:
        """Tell the hub the connection is gone. Idempotent: the slot is released once."""
        if self._detached:
            return
        self._detached = True
        try:
            self._state.hub.disconnected(self)
        except Exception as exc:
            log_exception(logger, "ws disconnect", exc)

    def stop(self) -> None:
        """Abandon the connection without a close frame. Idempotent."""
        self._shut()
        if self._writer is not None:
            self._writer.cancel()

    def _shut(self) -> None:
        self._open = False
        self._state.limits.free(self._outbox_bytes)
        self._state.outboxes.change(self.key, self, -self._outbox_bytes)
        self._outbox.clear()
        self._outbox_bytes = 0
        self._stopped.set()

    async def run(self) -> None:
        """Read until the client leaves or the connection is closed.

        After close(code) the closing handshake gets at most the close window, then the
        transport is aborted.
        """
        self._writer = asyncio.create_task(self._write())
        reader = asyncio.create_task(self._read())
        stopped = asyncio.create_task(self._stopped.wait())
        try:
            await asyncio.wait((reader, stopped), return_when=asyncio.FIRST_COMPLETED)
            if self._close_code is not None:
                await self._finish_close(self._writer)
        finally:
            self.stop()
            reader.cancel()
            stopped.cancel()
        if reader.done() and not reader.cancelled():
            exc = reader.exception()
            if exc is not None:
                raise exc

    async def _finish_close(self, writer: asyncio.Task[None]) -> None:
        """Wait for the close frame and the client's reply until the window ends, then abort.

        The abort does nothing to a transport that has already closed; it drops one whose
        buffer a client that does not read never drains. The handler is still running, so the
        connection keeps counting against the limits until its transport is gone.
        """
        window = asyncio.create_task(asyncio.sleep(self._state.close_window))
        try:
            await asyncio.wait((writer, window), return_when=asyncio.FIRST_COMPLETED)
            if self._abort is not None:
                await window
                self._abort()
        finally:
            window.cancel()

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
            # A batch of frames is read without suspending: let the writers drain in between.
            await asyncio.sleep(0)

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
                    self._state.outboxes.change(self.key, self, -size)
                if not self._open:
                    if self._close_code is not None:
                        await self._ws.close(self._close_code)
                    return
        except _GONE:
            self._shut()
            self.detach()  # the client is gone: its place waits from now on
        except Exception as exc:
            log_exception(logger, "ws writer", exc)
            self._shut()
            self.detach()


async def serve(
    websocket: Socket, host: str | None, state: WsState, *, abort: Abort | None = None
) -> None:
    """Run one client connection. Never raises: errors are logged by type name only.

    `abort` tears the transport down (see transport_abort). However the handler ends, the
    transport gets at most the close window more to close on its own.
    """
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
    conn = Connection(websocket, key, state, abort=abort)
    try:
        await websocket.accept()
        await conn.run()
    except Exception as exc:
        log_exception(logger, "ws handler", exc)
    finally:
        # Synchronous on purpose: a cancelled handler still releases its slot exactly once.
        conn.detach()
        state.limits.connection_closed()
        conn.stop()
        if abort is not None:
            asyncio.get_running_loop().call_later(state.close_window, abort)


router: Final = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    state: WsState = websocket.app.state.ws
    host = websocket.client.host if websocket.client else None
    abort = transport_abort(websocket.scope)
    if abort is None and not state.warned_no_abort:
        state.warned_no_abort = True
        logger.warning(NO_ABORT_WARNING)
    await serve(websocket, host, state, abort=abort)
