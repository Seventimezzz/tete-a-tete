"""Our uvicorn WebSocket protocol (H1): raw budget, PONG backpressure, keepalive abort, and the
abort hook for the application. Driven with a fake transport, no sockets."""

import asyncio
import inspect
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from uvicorn.config import Config
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from uvicorn.server import ServerState

from app import wsproto
from app.frames import RAW_FRAMES_BURST, RAW_FRAMES_RATE
from app.ws import ABORT_EXTENSION
from app.wsproto import PONG_BUFFER_LIMIT, UVICORN_VERSION, TeteWebSocketProtocol
from tests.fakes import FakeClock
from tests.test_frames import REQUEST, frame

PING = frame(0x9, b"")
PONG_HEAD = b"\x8a"


class FakeTransport:
    """asyncio.Transport double: records writes; abort reports the loss soon, like asyncio."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.aborted = 0
        self.closed = False
        self.buffered = 0
        self.protocol: asyncio.Protocol | None = None

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {"peername": ("203.0.113.9", 50000), "sockname": ("127.0.0.1", 8080)}.get(
            name, default
        )

    def write(self, data: bytes) -> None:
        self.written += data

    def get_write_buffer_size(self) -> int:
        return self.buffered

    def is_closing(self) -> bool:
        return self.closed or self.aborted > 0

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        if not self.is_closing() and self.protocol is not None:
            asyncio.get_running_loop().call_soon(self.protocol.connection_lost, None)
        self.aborted += 1

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


class App:
    """ASGI app: accepts, then records what it receives until the disconnect."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.done = asyncio.Event()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        self.scopes.append(scope)
        await receive()  # websocket.connect
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            self.received.append(message)
            if message["type"] == "websocket.disconnect":
                self.done.set()
                return


async def connect(*, ping: float | None = None) -> tuple[TeteWebSocketProtocol, FakeTransport, App]:
    app = App()
    config = Config(app=app, ws_ping_interval=ping, ws_ping_timeout=ping, ws_max_size=65_536)
    protocol = TeteWebSocketProtocol(config=config, server_state=ServerState(), app_state={})
    transport = FakeTransport()
    transport.protocol = protocol
    protocol.connection_made(transport)  # type: ignore[arg-type]
    protocol.data_received(REQUEST)
    async with asyncio.timeout(2.0):
        while not protocol.handshake_complete:  # noqa: ASYNC110 - state of a plain object
            await asyncio.sleep(0)
    transport.written.clear()
    return protocol, transport, app


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(TeteWebSocketProtocol, "clock", fake)
    return fake


@pytest.mark.usefixtures("clock")
async def test_ping_flood_is_cut_before_parsing() -> None:
    protocol, transport, app = await connect()
    protocol.data_received(PING * (RAW_FRAMES_BURST + 1))
    assert transport.aborted == 1
    assert PONG_HEAD not in transport.written  # the chunk was never parsed
    await asyncio.wait_for(app.done.wait(), 1.0)
    protocol.data_received(PING)  # nothing more is looked at
    assert transport.aborted == 1


@pytest.mark.usefixtures("clock")
async def test_one_byte_fragment_flood_is_cut() -> None:
    protocol, transport, app = await connect()
    protocol.data_received(frame(0x1, b"a", fin=False))
    for _ in range(RAW_FRAMES_BURST):
        if transport.aborted:
            break
        protocol.data_received(frame(0x0, b"b", fin=False))
    assert transport.aborted == 1
    await asyncio.wait_for(app.done.wait(), 1.0)
    assert [m["type"] for m in app.received] == ["websocket.disconnect"]


async def test_frames_refill_with_the_clock(clock: FakeClock) -> None:
    protocol, transport, _ = await connect()
    protocol.data_received(PING * (RAW_FRAMES_BURST - 1))
    clock.advance(1.0)
    protocol.data_received(PING * int(RAW_FRAMES_RATE))
    assert transport.aborted == 0
    assert transport.written.count(b"\x8a\x00") == RAW_FRAMES_BURST - 1 + int(RAW_FRAMES_RATE)


@pytest.mark.usefixtures("clock")
async def test_messages_reach_the_app() -> None:
    protocol, transport, app = await connect()
    protocol.data_received(frame(0x1, b'{"type":"ping"}'))
    async with asyncio.timeout(1.0):
        while not app.received:  # noqa: ASYNC110 - state of a plain object
            await asyncio.sleep(0)
    assert app.received == [{"type": "websocket.receive", "text": '{"type":"ping"}'}]
    assert transport.aborted == 0


@pytest.mark.usefixtures("clock")
async def test_no_pong_while_the_write_buffer_is_above_the_limit() -> None:
    protocol, transport, _ = await connect()
    transport.buffered = PONG_BUFFER_LIMIT + 1
    protocol.data_received(PING * 10)
    assert PONG_HEAD not in transport.written
    transport.buffered = PONG_BUFFER_LIMIT
    protocol.data_received(PING)
    assert transport.written == b"\x8a\x00"
    assert transport.aborted == 0


@pytest.mark.usefixtures("clock")
async def test_dropping_pongs_keeps_the_close_reply() -> None:
    protocol, transport, _ = await connect()
    transport.buffered = PONG_BUFFER_LIMIT + 1
    protocol.data_received(PING + frame(0x8, b"\x03\xe8"))  # PING then CLOSE 1000
    assert PONG_HEAD not in transport.written
    assert transport.written.startswith(b"\x88\x02\x03\xe8")
    assert transport.closed


@pytest.mark.usefixtures("clock")
async def test_keepalive_timeout_aborts_and_the_app_sees_the_disconnect() -> None:
    protocol, transport, app = await connect()
    transport.buffered = 10_000_000  # a frozen client: close() would wait for this forever
    protocol.keepalive_timeout()
    assert transport.aborted == 1
    await asyncio.wait_for(app.done.wait(), 1.0)
    assert app.received[-1]["type"] == "websocket.disconnect"


@pytest.mark.usefixtures("clock")
async def test_keepalive_timer_aborts_a_client_that_does_not_answer() -> None:
    protocol, transport, app = await connect(ping=0.02)
    await asyncio.wait_for(app.done.wait(), 2.0)
    assert transport.written.startswith(b"\x89\x04")  # the server's ping went out
    assert transport.aborted == 1
    assert protocol.disconnected


@pytest.mark.usefixtures("clock")
async def test_scope_carries_the_abort_hook() -> None:
    _, transport, app = await connect()
    hook = app.scopes[0]["extensions"][ABORT_EXTENSION]["abort"]
    hook()
    assert transport.aborted == 1
    await asyncio.wait_for(app.done.wait(), 1.0)


@pytest.mark.usefixtures("clock")
async def test_refused_handshake_has_no_hook_and_no_task() -> None:
    app = App()
    config = Config(app=app, ws_max_size=65_536)
    protocol = TeteWebSocketProtocol(config=config, server_state=ServerState(), app_state={})
    transport = FakeTransport()
    transport.protocol = protocol
    protocol.connection_made(transport)  # type: ignore[arg-type]
    protocol.data_received(REQUEST.replace(b"Version: 13", b"Version: 99"))
    await asyncio.sleep(0)
    assert transport.written.startswith(b"HTTP/1.1 400")
    assert app.scopes == []


# --- the uvicorn internals the subclass relies on (fails loudly on an upgrade)


def test_uvicorn_is_the_pinned_version() -> None:
    assert uvicorn.__version__ == UVICORN_VERSION
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8"))
    assert f"uvicorn[standard]=={UVICORN_VERSION}" in project["project"]["dependencies"]


@pytest.mark.parametrize(
    "name",
    [
        "data_received",
        "handle_events",
        "handle_ping",
        "handle_connect",
        "keepalive_timeout",
        "connection_lost",
        "send_keepalive_ping",
    ],
)
def test_overridden_and_used_methods_exist_on_the_base(name: str) -> None:
    assert callable(vars(WebSocketsSansIOProtocol).get(name))


def test_base_internals_still_look_as_the_subclass_expects() -> None:
    params = list(inspect.signature(WebSocketsSansIOProtocol.__init__).parameters)
    assert params == ["self", "config", "server_state", "app_state", "_loop"]
    events = inspect.getsource(WebSocketsSansIOProtocol.handle_events)
    assert "self.handle_ping()" in events
    connect_src = inspect.getsource(WebSocketsSansIOProtocol.handle_connect)
    assert "self.scope" in connect_src
    assert '"extensions"' in connect_src
    timeout_src = inspect.getsource(WebSocketsSansIOProtocol.keepalive_timeout)
    assert "self.pong_timer = None" in timeout_src
    assert "self.transport.close()" in timeout_src  # what we replace with abort
    ping_src = inspect.getsource(WebSocketsSansIOProtocol.handle_ping)
    assert "self.conn.data_to_send()" in ping_src
    assert wsproto.TeteWebSocketProtocol.__mro__[1] is WebSocketsSansIOProtocol
