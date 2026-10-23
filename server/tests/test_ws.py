import asyncio
import json
import logging
import time
import warnings
from typing import Any

import pytest
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app import main as main_module
from app import ws as ws_module
from app.config import ConfigError, Settings
from app.limits import LimitConfig, Limits
from app.logs import loop_exception_handler
from app.main import create_app
from app.protocol import ClientMessage
from app.rooms import Conn, Hub
from app.ws import (
    OUTBOX_MAX_BYTES,
    OUTBOX_MAX_MESSAGES,
    Connection,
    WsState,
    serve,
    transport_aborter,
)
from tests.fakes import FakeClock

# Starlette 1.9 prefers httpx2 for TestClient; httpx works, and no new dependencies are added.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from starlette.testclient import TestClient

SECRET = b"t" * 32
NET = {"nat": None, "upnp": None, "cgnat": None, "doubleNat": None, "vpn": None}
LEAKY = "Аня abcdefghijkm tok.en 203.0.113.7"
WARNING_TEXT = "X-Forwarded-For отсутствует, лимиты общие"


def make_client(
    *,
    clock: FakeClock | None = None,
    limit_config: LimitConfig | None = None,
    host: str = "testclient",
    hub_factory: type[Hub] = Hub,
) -> TestClient:
    app = create_app(
        Settings(SECRET),
        clock=clock or FakeClock(),
        limit_config=limit_config,
        hub_factory=hub_factory,
    )
    return TestClient(app, client=(host, 50000))


def state_of(client: TestClient) -> WsState:
    state: WsState = client.app.state.ws  # type: ignore[attr-defined]
    return state


def wait_until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def create_msg(name: str = "Аня") -> str:
    return json.dumps({"type": "create", "name": name, "net": NET})


def join_msg(room_id: str, name: str = "Борис", token: str | None = None) -> str:
    msg: dict[str, Any] = {"type": "join", "roomId": room_id, "name": name, "net": NET}
    if token is not None:
        msg["resumeToken"] = token
    return json.dumps(msg)


PING = json.dumps({"type": "ping"})


# --- endpoint through Starlette TestClient


def test_ping_pong() -> None:
    with make_client() as client, client.websocket_connect("/ws") as ws:
        ws.send_text(PING)
        assert ws.receive_json() == {"type": "pong"}


def test_bad_json_and_binary_get_bad_request_and_connection_stays() -> None:
    with make_client() as client, client.websocket_connect("/ws") as ws:
        ws.send_text("{not json")
        assert ws.receive_json() == {"type": "error", "code": "bad-request"}
        ws.send_text(json.dumps({"type": "ping", "extra": 1}))
        assert ws.receive_json() == {"type": "error", "code": "bad-request"}
        ws.send_bytes(PING.encode())
        assert ws.receive_json() == {"type": "error", "code": "bad-request"}
        ws.send_text(PING)
        assert ws.receive_json() == {"type": "pong"}


def test_two_clients_create_join_signal() -> None:
    with (
        make_client() as client,
        client.websocket_connect("/ws") as a,
        client.websocket_connect("/ws") as b,
    ):
        a.send_text(create_msg())
        joined_a = a.receive_json()
        assert joined_a["type"] == "joined"
        b.send_text(join_msg(joined_a["roomId"]))
        joined_b = b.receive_json()
        assert joined_b["resumed"] is False
        assert [p["id"] for p in joined_b["peers"]] == [joined_a["selfId"]]
        assert a.receive_json() == {
            "type": "peer-joined",
            "id": joined_b["selfId"],
            "name": "Борис",
        }
        data = {"description": {"type": "offer", "sdp": "v=0 кириллица"}}
        b.send_text(json.dumps({"type": "signal", "to": joined_a["selfId"], "data": data}))
        assert a.receive_json() == {"type": "signal", "from": joined_b["selfId"], "data": data}


def test_handshake_refused_over_seat_limit() -> None:
    with (
        make_client(limit_config=LimitConfig(seats_per_key=1)) as client,
        client.websocket_connect("/ws") as first,
    ):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
            pass
        state = state_of(client)
        assert state.limits.connections == 1
        assert state.closed_by_limits == 1
        first.send_text(PING)
        assert first.receive_json() == {"type": "pong"}


def test_slot_released_after_disconnect() -> None:
    with make_client(limit_config=LimitConfig(seats_per_key=1)) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(PING)
            ws.receive_json()
        state = state_of(client)
        assert wait_until(lambda: state.limits.connections == 0)
        assert state.limits.seats("unknown") == 0
        with client.websocket_connect("/ws") as ws:
            ws.send_text(PING)
            assert ws.receive_json() == {"type": "pong"}


def test_message_rate_limit_closes_1008() -> None:
    config = LimitConfig(msg_burst=5, msg_rate=0.0001)
    with make_client(limit_config=config) as client, client.websocket_connect("/ws") as ws:
        for _ in range(5):
            ws.send_text(PING)
            assert ws.receive_json() == {"type": "pong"}
        ws.send_text(PING)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008
        assert state_of(client).closed_by_limits == 1


def test_bad_messages_count_against_rate_limit() -> None:
    config = LimitConfig(msg_burst=2, msg_rate=0.0001)
    with make_client(limit_config=config) as client, client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"\x00")
        ws.receive_json()
        ws.send_text("{")
        ws.receive_json()
        ws.send_text("{")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008


def test_byte_rate_limit_closes_1008() -> None:
    config = LimitConfig(bytes_burst=100, bytes_rate=0.0001)
    with make_client(limit_config=config) as client, client.websocket_connect("/ws") as ws:
        ws.send_text("x" * 101)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008


def test_key_byte_limit_shared_by_connections() -> None:
    config = LimitConfig(key_bytes_burst=100, key_bytes_rate=0.0001)
    with (
        make_client(limit_config=config) as client,
        client.websocket_connect("/ws") as a,
        client.websocket_connect("/ws") as b,
    ):
        a.send_text("x" * 60)
        assert a.receive_json()["code"] == "bad-request"
        b.send_text("x" * 60)
        with pytest.raises(WebSocketDisconnect) as closed:
            b.receive_json()
        assert closed.value.code == 1008


def test_replacing_join_closes_old_connection_4000() -> None:
    with (
        make_client() as client,
        client.websocket_connect("/ws") as a,
        client.websocket_connect("/ws") as b,
    ):
        a.send_text(create_msg())
        joined = a.receive_json()
        b.send_text(join_msg(joined["roomId"], token=joined["resumeToken"]))
        rejoined = b.receive_json()
        assert rejoined["resumed"] is True
        assert rejoined["selfId"] == joined["selfId"]
        with pytest.raises(WebSocketDisconnect) as closed:
            a.receive_json()
        assert closed.value.code == 4000
        assert state_of(client).closed_by_limits == 0


def test_missing_secret_stops_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("RESUME_SECRET", raising=False)
    with pytest.raises(ConfigError), TestClient(create_app()):
        pass
    assert "RESUME_SECRET is missing" in caplog.text


def test_short_secret_stops_startup_without_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("RESUME_SECRET", "short-secret-value")
    with pytest.raises(ConfigError), TestClient(create_app()):
        pass
    assert "RESUME_SECRET" in caplog.text
    assert "short-secret-value" not in caplog.text


def test_secret_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESUME_SECRET", "s" * 32)
    with TestClient(create_app()) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(PING)
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize("host", ["testclient", "127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_forwarded_for_warning_once_without_address(
    caplog: pytest.LogCaptureFixture, host: str
) -> None:
    caplog.set_level(logging.WARNING)
    with make_client(host=host) as client:
        for _ in range(2):
            with client.websocket_connect("/ws") as ws:
                ws.send_text(PING)
                ws.receive_json()
    warnings = [r for r in caplog.records if r.getMessage() == WARNING_TEXT]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    for leak in (host, "127.0.0.1", "testclient"):
        assert leak not in caplog.text


def test_no_forwarded_for_warning_for_public_address(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    with make_client(host="203.0.113.7") as client, client.websocket_connect("/ws") as ws:
        ws.send_text(PING)
        ws.receive_json()
    assert WARNING_TEXT not in caplog.text
    assert "203.0.113.7" not in caplog.text


def test_sweeper_runs_in_lifespan() -> None:
    clock = FakeClock()
    with make_client(clock=clock) as client, client.websocket_connect("/ws") as a:
        state = state_of(client)
        a.send_text(create_msg())
        joined = a.receive_json()
        with client.websocket_connect("/ws") as b:
            b.send_text(join_msg(joined["roomId"]))
            peer_b = b.receive_json()["selfId"]
            a.receive_json()  # peer-joined
        assert wait_until(lambda: state.hub.stats().waiting == 1)
        clock.advance(15.0)
        assert wait_until(lambda: state.hub.stats().waiting == 0)
        assert a.receive_json() == {"type": "peer-left", "id": peer_b}


def test_sweeper_sweeps_limits_and_logs_stats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(main_module, "SWEEP_SECONDS", 0.02)
    caplog.set_level(logging.INFO, logger="tete")
    swept: list[int] = []
    monkeypatch.setattr(Limits, "sweep", lambda self: swept.append(1))
    clock = FakeClock()
    with make_client(clock=clock) as client, client.websocket_connect("/ws") as ws:
        assert wait_until(lambda: len(swept) >= 2)
        assert "stats " not in caplog.text
        clock.advance(60.0)
        assert wait_until(
            lambda: (
                "stats rooms=0 peers=0 waiting=0 connections=1 closed_by_limits=0" in caplog.text
            )
        )
        ws.send_text(PING)
        ws.receive_json()


class FailingSweepHub(Hub):
    calls = 0

    def sweep(self) -> None:
        FailingSweepHub.calls += 1
        raise ValueError(LEAKY)


def test_sweeper_exception_logged_by_type_and_loop_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(main_module, "SWEEP_SECONDS", 0.02)
    FailingSweepHub.calls = 0
    with make_client(hub_factory=FailingSweepHub):
        assert wait_until(lambda: FailingSweepHub.calls >= 3)
    assert "sweeper failed: ValueError" in caplog.text
    for leak in ("Аня", "abcdefghijkm", "tok.en", "203.0.113.7", "Traceback"):
        assert leak not in caplog.text


def test_loop_exception_handler_installed_in_lifespan() -> None:
    def current() -> object:
        return asyncio.get_running_loop().get_exception_handler()

    with make_client() as client:
        assert client.portal is not None
        assert client.portal.call(current) is loop_exception_handler


# --- Connection and serve with a fake socket


class FakeSocket:
    """Starlette WebSocket double: incoming queue, recorded sends and close code."""

    def __init__(self, *, stuck_send: bool = False) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed_with: int | None = None
        self.accepted = False
        self._stuck_send = stuck_send

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_text(self, data: str) -> None:
        if self._stuck_send:
            await asyncio.Event().wait()
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = code

    def push(self, text: str) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": text})

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(t) for t in self.sent]


class SpyHub(Hub):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.handled: list[tuple[Conn, str]] = []
        self.disconnects: list[Conn] = []

    def handle(self, conn: Conn, message: ClientMessage) -> None:
        self.handled.append((conn, message.type))
        super().handle(conn, message)

    def disconnected(self, conn: Conn) -> None:
        self.disconnects.append(conn)
        super().disconnected(conn)


def make_state(config: LimitConfig | None = None) -> tuple[WsState, SpyHub]:
    clock = FakeClock()
    limits = Limits(config or LimitConfig(), clock)
    hub = SpyHub(secret=SECRET, clock=clock, limits=limits)
    return WsState(hub=hub, limits=limits, clock=clock), hub


async def until(predicate: Any) -> None:
    """Poll a condition set by other tasks; at most 2 s."""
    async with asyncio.timeout(2.0):
        while not predicate():  # noqa: ASYNC110 - state of plain objects, no event to wait on
            await asyncio.sleep(0.001)


@pytest.fixture
def fast_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "CLOSE_WAIT_SECONDS", 0.05)


@pytest.mark.usefixtures("fast_close")
async def test_outbox_overflow_closes_4001() -> None:
    state, _ = make_state()
    sock = FakeSocket(stuck_send=True)
    conn = Connection(sock, "k", state)
    run = asyncio.create_task(conn.run())
    for _ in range(OUTBOX_MAX_MESSAGES):
        conn.send("{}")
    await asyncio.sleep(0)
    assert not conn.closed
    assert state.limits.queued_bytes == 2 * OUTBOX_MAX_MESSAGES
    conn.send("{}")
    assert conn.closed
    assert state.limits.queued_bytes == 0
    assert state.closed_by_limits == 1
    await asyncio.wait_for(run, 1.0)  # the close frame is stuck too: run returns anyway
    conn.send("{}")
    assert state.limits.queued_bytes == 0


@pytest.mark.usefixtures("fast_close")
async def test_outbox_byte_overflow_closes_4001() -> None:
    state, _ = make_state()
    sock = FakeSocket(stuck_send=True)
    conn = Connection(sock, "k", state)
    run = asyncio.create_task(conn.run())
    chunk = "x" * 65_536
    for _ in range(OUTBOX_MAX_BYTES // len(chunk)):
        conn.send(chunk)
    await asyncio.sleep(0)
    assert not conn.closed
    conn.send(chunk)
    assert conn.closed
    assert state.limits.queued_bytes == 0
    await asyncio.wait_for(run, 1.0)


async def test_outbox_overflow_on_global_queue_limit() -> None:
    state, _ = make_state(LimitConfig(max_queued_bytes=10))
    conn = Connection(FakeSocket(stuck_send=True), "k", state)
    conn.send("x" * 10)
    assert not conn.closed
    conn.send("x")
    assert conn.closed
    assert state.limits.queued_bytes == 0


async def test_writer_sends_in_order_and_frees_bytes() -> None:
    state, _ = make_state()
    sock = FakeSocket()
    conn = Connection(sock, "k", state)
    run = asyncio.create_task(conn.run())
    for i in range(5):
        conn.send(f'"{i}"')
    await until(lambda: len(sock.sent) == 5)
    assert sock.sent == ['"0"', '"1"', '"2"', '"3"', '"4"']
    assert state.limits.queued_bytes == 0
    sock.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(run, 1.0)
    assert sock.closed_with is None


async def test_close_sends_code_and_ignores_later_sends() -> None:
    state, _ = make_state()
    sock = FakeSocket()
    conn = Connection(sock, "k", state)
    run = asyncio.create_task(conn.run())
    conn.close(4000)
    conn.send('"late"')
    await asyncio.wait_for(run, 1.0)
    assert sock.closed_with == 4000
    assert sock.sent == []


async def test_serve_releases_once_and_never_for_refused_handshake() -> None:
    state, hub = make_state(LimitConfig(seats_per_key=1))
    first = FakeSocket()
    task = asyncio.create_task(serve(first, "198.51.100.1", state))
    await until(lambda: first.accepted)
    refused = FakeSocket()
    await serve(refused, "198.51.100.1", state)
    assert not refused.accepted
    assert refused.closed_with == 1000
    assert state.limits.connections == 1
    first.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 1.0)
    assert len(hub.disconnects) == 1
    assert state.limits.connections == 0
    assert state.limits.seats("198.51.100.1") == 0


async def test_replaced_connection_stops_dispatching() -> None:
    state, hub = make_state()
    a, b = FakeSocket(), FakeSocket()
    task_a = asyncio.create_task(serve(a, "198.51.100.1", state))
    a.push(create_msg())
    await until(lambda: len(a.sent) == 1)
    joined = a.messages()[0]
    task_b = asyncio.create_task(serve(b, "198.51.100.2", state))
    await until(lambda: b.accepted)
    await asyncio.sleep(0)
    # Same tick: B's join closes A, then A's reader wakes with a message already read.
    b.push(join_msg(joined["roomId"], token=joined["resumeToken"]))
    a.push(json.dumps({"type": "mute", "muted": True}))
    await asyncio.wait_for(task_a, 1.0)
    assert a.closed_with == 4000
    conn_a = hub.handled[0][0]
    assert [t for c, t in hub.handled if c is conn_a] == ["create"]
    assert hub.disconnects == [conn_a]
    assert b.messages()[0]["resumed"] is True
    b.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task_b, 1.0)
    assert state.limits.connections == 0


class ExplodingHub(SpyHub):
    def handle(self, conn: Conn, message: ClientMessage) -> None:
        raise ValueError(LEAKY)


async def test_handler_exception_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    limits = Limits(LimitConfig(), clock)
    hub = ExplodingHub(secret=SECRET, clock=clock, limits=limits)
    state = WsState(hub=hub, limits=limits, clock=clock)
    sock = FakeSocket()
    task = asyncio.create_task(serve(sock, "198.51.100.1", state))
    sock.push(PING)
    await asyncio.wait_for(task, 1.0)
    assert "ws handler failed: ValueError" in caplog.text
    for leak in ("Аня", "abcdefghijkm", "tok.en", "203.0.113.7", "198.51.100.1", "Traceback"):
        assert leak not in caplog.text
    assert hub.disconnects != []
    assert limits.connections == 0


# --- a connection the server closes is gone for the hub at once (QA-4)


@pytest.fixture
def slow_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "CLOSE_WAIT_SECONDS", 0.5)


@pytest.mark.usefixtures("slow_close")
async def test_server_close_starts_the_wait_at_once_and_reports_once() -> None:
    state, hub = make_state(LimitConfig(msg_burst=2, msg_rate=0.0001))
    a, b = FakeSocket(), FakeSocket(stuck_send=True)  # b never reads: its close frame hangs
    task_a = asyncio.create_task(serve(a, "198.51.100.1", state))
    a.push(create_msg())
    await until(lambda: len(a.sent) == 1)
    task_b = asyncio.create_task(serve(b, "198.51.100.2", state))
    b.push(join_msg(a.messages()[0]["roomId"]))
    await until(lambda: len(hub.handled) == 2)
    conn_b = hub.handled[1][0]
    b.push(PING)
    b.push(PING)  # over the burst of 2: closed with 1008
    await until(lambda: hub.disconnects != [])
    assert not task_b.done()  # the handler still waits for the close frame
    assert hub.disconnects == [conn_b]
    assert hub.stats().waiting == 1
    assert state.limits.connections == 2  # the connection itself is not closed yet
    await asyncio.wait_for(task_b, 2.0)
    assert hub.disconnects == [conn_b]  # not reported again by the handler
    assert state.limits.connections == 1
    assert state.limits.seats("198.51.100.2") == 1  # the waiting place keeps the slot
    a.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task_a, 2.0)
    assert state.limits.connections == 0


@pytest.mark.usefixtures("fast_close")
async def test_server_close_outside_a_room_releases_the_slot_once() -> None:
    state, hub = make_state(LimitConfig(msg_burst=1, msg_rate=0.0001, seats_per_key=1))
    sock = FakeSocket()
    task = asyncio.create_task(serve(sock, "198.51.100.1", state))
    sock.push(PING)
    sock.push(PING)
    await asyncio.wait_for(task, 2.0)
    assert sock.closed_with == 1008
    assert len(hub.disconnects) == 1
    assert state.limits.seats("198.51.100.1") == 0
    assert state.limits.connections == 0


# --- the transport behind a server-initiated close is torn down (QA-2)


class AbortSpy:
    """Stands for transport.abort; records how many connections were counted at each call."""

    def __init__(self, state: WsState) -> None:
        self._state = state
        self.calls: list[int] = []

    def __call__(self) -> None:
        self.calls.append(self._state.limits.connections)


@pytest.mark.usefixtures("fast_close")
@pytest.mark.parametrize("stuck", [True, False], ids=["frame-stuck", "frame-left"])
async def test_server_close_aborts_the_transport_at_the_end_of_the_window(stuck: bool) -> None:
    """Whether or not the close frame left, the transport is aborted once the window ends,
    while the connection still counts: the slot is not free before the transport is gone."""
    state, _ = make_state(LimitConfig(msg_burst=1, msg_rate=0.0001))
    sock = FakeSocket(stuck_send=stuck)
    abort = AbortSpy(state)
    task = asyncio.create_task(serve(sock, "198.51.100.1", state, abort=abort))
    sock.push(PING)
    sock.push(PING)  # over the burst: closed with 1008
    await until(lambda: abort.calls != [])
    assert abort.calls == [1]
    await asyncio.wait_for(task, 1.0)
    assert state.limits.connections == 0
    await asyncio.sleep(ws_module.CLOSE_WAIT_SECONDS * 2)
    assert len(abort.calls) <= 2  # at most the backstop after the handler, never more


@pytest.mark.usefixtures("fast_close")
async def test_handler_end_schedules_a_backstop_abort() -> None:
    """Any end of the handler (here a failing hub) leaves the transport at most the window."""
    clock = FakeClock()
    limits = Limits(LimitConfig(), clock)
    hub = ExplodingHub(secret=SECRET, clock=clock, limits=limits)
    state = WsState(hub=hub, limits=limits, clock=clock)
    sock = FakeSocket()
    abort = AbortSpy(state)
    task = asyncio.create_task(serve(sock, "198.51.100.1", state, abort=abort))
    sock.push(PING)
    await asyncio.wait_for(task, 1.0)
    assert abort.calls == []
    await until(lambda: abort.calls != [])
    assert abort.calls == [0]


async def test_client_close_does_not_wait_for_the_window() -> None:
    state, _ = make_state()
    sock = FakeSocket()
    abort = AbortSpy(state)
    task = asyncio.create_task(serve(sock, "198.51.100.1", state, abort=abort))
    await until(lambda: sock.accepted)
    sock.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, ws_module.CLOSE_WAIT_SECONDS / 2)
    assert abort.calls == []


class _Transport:
    def __init__(self) -> None:
        self.aborted = 0

    def abort(self) -> None:
        self.aborted += 1

    def is_closing(self) -> bool:
        return False


class _UvicornProtocol:
    def __init__(self) -> None:
        self.transport = _Transport()

    async def receive(self) -> dict[str, Any]:
        return {}


class _StarletteSocket:
    def __init__(self, receive: Any) -> None:
        self._receive = receive


def test_transport_aborter_reaches_the_transport_behind_the_receive_callable() -> None:
    protocol = _UvicornProtocol()
    abort = transport_aborter(_StarletteSocket(protocol.receive))
    assert abort is not None
    abort()
    assert protocol.transport.aborted == 1


@pytest.mark.parametrize(
    "receive",
    [None, print, _UvicornProtocol.receive, _Transport().is_closing],
    ids=["none", "builtin", "unbound", "no-transport"],
)
def test_transport_aborter_falls_back_to_none(receive: Any) -> None:
    assert transport_aborter(_StarletteSocket(receive)) is None
    assert transport_aborter(object()) is None


def test_transport_aborter_is_none_under_the_test_client() -> None:
    seen: list[object] = []
    app = create_app(Settings(SECRET), clock=FakeClock())

    @app.websocket("/probe")
    async def probe(websocket: WebSocket) -> None:
        seen.append(transport_aborter(websocket))
        await websocket.close()

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/probe"),
    ):
        pass
    assert seen == [None]
