"""QA, stage 1: the /ws connection layer with a fake socket and fake clock.

Boundaries of the default rate limits, server-initiated closes, cancellation and failures,
and that nothing personal reaches the log on those paths.
"""

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import Any

import pytest

from app import ws as ws_module
from app.limits import LimitConfig, Limits
from app.protocol import CLOSE_RATE_LIMIT
from app.rooms import Hub, HubStats
from app.ws import WsState, serve
from tests.fakes import FakeClock

SECRET = b"w" * 32
NET = {"nat": None, "upnp": None, "cgnat": None, "doubleNat": None, "vpn": None}
PING = json.dumps({"type": "ping"})
IP_A = "198.51.100.31"
IP_B = "198.51.100.32"
LEAK = "Утечка abcdefghjkmn 203.0.113.77 v=0 a=candidate"


class Sock:
    """Starlette WebSocket double. `stuck_close` never finishes the close frame."""

    def __init__(self, *, stuck_close: bool = False, send_error: Exception | None = None) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.close_started: int | None = None
        self.closed_with: int | None = None
        self._stuck_close = stuck_close
        self._send_error = send_error

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_text(self, data: str) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_started = code
        if self._stuck_close:
            await asyncio.Event().wait()
        self.closed_with = code

    def push(self, text: str) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": text})

    def push_bytes(self, data: bytes) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def hang_up(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1006})

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]


def fake_clock(state: WsState) -> FakeClock:
    assert isinstance(state.clock, FakeClock)
    return state.clock


def make_state(config: LimitConfig | None = None) -> WsState:
    clock = FakeClock()
    limits = Limits(config or LimitConfig(), clock)
    return WsState(hub=Hub(secret=SECRET, clock=clock, limits=limits), limits=limits, clock=clock)


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:  # noqa: ASYNC109 - test helper
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110 - plain objects changed by other tasks
            await asyncio.sleep(0.001)


def join_msg(room_id: str, name: str = "Борис", token: str | None = None) -> str:
    msg: dict[str, Any] = {"type": "join", "roomId": room_id, "name": name, "net": NET}
    if token is not None:
        msg["resumeToken"] = token
    return json.dumps(msg)


CREATE = json.dumps({"type": "create", "name": "Аня", "net": NET})


@pytest.fixture
def short_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "CLOSE_WAIT_SECONDS", 0.3)


# --- default rate limits at their boundaries


async def test_default_message_burst_200_passes_201st_closes_1008() -> None:
    state = make_state()
    sock = Sock()
    task = asyncio.create_task(serve(sock, IP_A, state))
    for _ in range(200):
        sock.push(PING)
    await until(lambda: len(sock.sent) == 200)
    assert sock.closed_with is None
    sock.push(PING)
    await asyncio.wait_for(task, 2.0)
    assert sock.closed_with == CLOSE_RATE_LIMIT
    assert len(sock.sent) == 200  # no error message before the close
    assert state.limits.connections == 0


async def test_message_bucket_refills_at_20_per_second() -> None:
    state = make_state()
    sock = Sock()
    task = asyncio.create_task(serve(sock, IP_A, state))
    for _ in range(200):
        sock.push(PING)
    await until(lambda: len(sock.sent) == 200)
    fake_clock(state).advance(0.051)  # a little over one token: 1000.0 + 0.05 rounds below it
    sock.push(PING)
    await until(lambda: len(sock.sent) == 201)
    sock.push(PING)
    await asyncio.wait_for(task, 2.0)
    assert sock.closed_with == CLOSE_RATE_LIMIT


async def test_default_byte_burst_256k_passes_then_one_byte_closes() -> None:
    state = make_state()
    sock = Sock()
    task = asyncio.create_task(serve(sock, IP_A, state))
    for _ in range(4):
        sock.push("x" * 65_536)  # 4 x 64 KB = 256 KB: bad-request each, still open
    await until(lambda: len(sock.sent) == 4)
    assert sock.types() == ["error"] * 4
    sock.push("x")
    await asyncio.wait_for(task, 2.0)
    assert sock.closed_with == CLOSE_RATE_LIMIT


async def test_default_key_byte_bucket_shared_by_two_connections() -> None:
    state = make_state()
    first, second = Sock(), Sock()
    t1 = asyncio.create_task(serve(first, IP_A, state))
    t2 = asyncio.create_task(serve(second, IP_A, state))
    for sock in (first, second):
        for _ in range(4):
            sock.push("y" * 65_536)
    await until(lambda: len(first.sent) == len(second.sent) == 4)  # 512 KB on the key
    other = Sock()
    t3 = asyncio.create_task(serve(other, IP_B, state))
    other.push(PING)
    await until(lambda: other.types() == ["pong"])  # another key is unaffected
    second.push(PING)  # 15 bytes over the key bucket
    await asyncio.wait_for(t2, 2.0)
    assert second.closed_with == CLOSE_RATE_LIMIT
    for sock in (first, other):
        sock.hang_up()
    await asyncio.gather(t1, t3)
    assert state.limits.connections == 0
    assert state.limits.seats(IP_A) == 0


async def test_binary_frames_count_bytes_too() -> None:
    state = make_state(LimitConfig(bytes_burst=100, bytes_rate=0.0001))
    sock = Sock()
    task = asyncio.create_task(serve(sock, IP_A, state))
    sock.push_bytes(b"\x00" * 100)
    await until(lambda: sock.types() == ["error"])
    sock.push_bytes(b"\x00")
    await asyncio.wait_for(task, 2.0)
    assert sock.closed_with == CLOSE_RATE_LIMIT


# --- server-initiated closes and the waiting place


@pytest.mark.usefixtures("short_close")
async def test_signal_to_a_connection_closed_by_the_server_is_queued() -> None:
    state = make_state(LimitConfig(msg_burst=3, msg_rate=0.0001))
    a, b = Sock(), Sock(stuck_close=True)
    task_a = asyncio.create_task(serve(a, IP_A, state))
    a.push(CREATE)  # a: message 1
    await until(lambda: a.types() == ["joined"])
    room_id = a.sent[0]["roomId"]
    task_b = asyncio.create_task(serve(b, IP_B, state))
    b.push(join_msg(room_id))  # b: message 1
    await until(lambda: b.types() == ["joined"] and a.types() == ["joined", "peer-joined"])
    b_id, b_token = b.sent[0]["selfId"], b.sent[0]["resumeToken"]
    for _ in range(3):  # b: messages 2, 3 and 4 - the 4th is over the burst
        b.push(PING)
    await until(lambda: b.close_started == CLOSE_RATE_LIMIT)
    # The server has closed b; until its handler ends, b's place is not waiting yet.
    a.push(json.dumps({"type": "signal", "to": b_id, "data": {"offer": "после-закрытия"}}))
    a.push(PING)  # a: messages 2 and 3
    await until(lambda: a.types()[-1:] == ["pong"])
    await asyncio.wait_for(task_b, 2.0)
    assert state.hub.stats() == HubStats(rooms=1, peers=2, waiting=1)

    b2 = Sock()
    task_b2 = asyncio.create_task(serve(b2, IP_B, state))
    b2.push(join_msg(room_id, token=b_token))
    b2.push(PING)
    await until(lambda: "pong" in b2.types())
    for sock in (a, b2):
        sock.hang_up()
    await asyncio.gather(task_a, task_b2)
    assert b2.types() == ["joined", "signal", "pong"]


@pytest.mark.usefixtures("short_close")
async def test_stuck_close_frame_still_releases_the_slot_once() -> None:
    state = make_state(LimitConfig(msg_burst=1, msg_rate=0.0001, seats_per_key=1))
    sock = Sock(stuck_close=True)
    task = asyncio.create_task(serve(sock, IP_A, state))
    sock.push(PING)
    sock.push(PING)
    await asyncio.wait_for(task, 2.0)  # CLOSE_WAIT_SECONDS, not forever
    assert sock.close_started == CLOSE_RATE_LIMIT
    assert state.limits.connections == 0
    assert state.limits.seats(IP_A) == 0
    assert state.closed_by_limits == 1


async def test_cancelled_handler_in_room_leaves_a_waiting_place_and_one_slot() -> None:
    state = make_state()
    a, b = Sock(), Sock()
    task_a = asyncio.create_task(serve(a, IP_A, state))
    a.push(CREATE)
    await until(lambda: a.types() == ["joined"])
    task_b = asyncio.create_task(serve(b, IP_A, state))
    b.push(join_msg(a.sent[0]["roomId"]))
    await until(lambda: b.types() == ["joined"])
    task_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_b
    assert state.limits.connections == 1
    assert state.limits.seats(IP_A) == 2  # a's connection and b's waiting place
    assert state.hub.stats() == HubStats(rooms=1, peers=2, waiting=1)
    a.hang_up()
    await task_a
    fake_clock(state).advance(15.0)
    state.hub.sweep()
    assert state.limits.seats(IP_A) == 0
    assert state.hub.stats() == HubStats(0, 0, 0)
    assert state.limits.queued_bytes == 0


async def test_writer_failure_logs_type_only_and_releases(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    state = make_state()
    sock = Sock(send_error=ValueError(LEAK))
    task = asyncio.create_task(serve(sock, IP_A, state))
    sock.push(CREATE)
    await asyncio.wait_for(task, 2.0)  # the writer died, the connection ends
    assert "ws writer failed: ValueError" in caplog.text
    for part in ("Утечка", "abcdefghjkmn", "203.0.113.77", IP_A, "candidate", "Traceback"):
        assert part not in caplog.text
    assert state.limits.connections == 0
    assert state.limits.queued_bytes == 0
    assert state.hub.stats().waiting == 1  # it was in a room: the place waits


async def test_refused_handshake_and_errors_log_no_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    state = make_state(LimitConfig(seats_per_key=1))
    held = Sock()
    task = asyncio.create_task(serve(held, IP_A, state))
    await until(lambda: held.accepted)
    refused = Sock()
    await serve(refused, IP_A, state)
    assert not refused.accepted
    held.push("{не json " + LEAK)
    held.push_bytes(LEAK.encode())
    await until(lambda: held.types() == ["error", "error"])
    held.hang_up()
    await task
    for part in ("Утечка", "abcdefghjkmn", "203.0.113.77", IP_A, "candidate"):
        assert part not in caplog.text


# --- R6 over real connections: every byte and slot comes back exactly once

CHURN_STEPS = 600
SENTINEL = 12_345  # reserved up front: a double free shows as less, a leak as more


async def test_churn_over_real_connections_returns_every_byte_and_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_module, "CLOSE_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(ws_module, "OUTBOX_MAX_MESSAGES", 6)  # stuck readers overflow fast
    big = 10**9
    state = make_state(
        LimitConfig(
            seats_per_key=big,
            handshakes_per_window=big,
            joins_per_window=big,
            creates_per_window=big,
            msg_burst=big,
            msg_rate=big,
            bytes_burst=big,
            bytes_rate=big,
            key_bytes_burst=big,
            key_bytes_rate=big,
        )
    )
    assert state.limits.reserve(SENTINEL)
    rng = random.Random(7)  # noqa: S311 - a reproducible test sequence
    keys = ["198.51.100.1", "198.51.100.2", "2001:db8::1"]
    room_ids = ["abcdefghjkmn", "bcdefghjkmnp"]
    live: list[tuple[Sock, asyncio.Task[None]]] = []
    every: list[asyncio.Task[None]] = []
    seen: list[Sock] = []
    stats = {"resumed": 0, "replaced": 0, "overflow": 0, "signals": 0}

    def tokens() -> list[tuple[str, str, str]]:
        return [
            (m["roomId"], m["resumeToken"], m["selfId"])
            for s in seen
            for m in s.sent
            if m["type"] == "joined"
        ]

    for _ in range(CHURN_STEPS):
        live = [(s, t) for s, t in live if not t.done()]
        op = rng.random()
        if op < 0.15 or not live:
            sock = Sock(stuck_close=rng.random() < 0.2)
            if rng.random() < 0.15:
                sock.send_text = _stuck_send  # type: ignore[method-assign]
            task = asyncio.create_task(serve(sock, rng.choice(keys), state))
            live.append((sock, task))
            every.append(task)
            seen.append(sock)
        else:
            sock, _task = rng.choice(live)
            known = tokens()
            if op < 0.3:
                if rng.random() < 0.3:
                    sock.push(CREATE)
                else:
                    sock.push(join_msg(rng.choice(room_ids)))
            elif op < 0.42 and known:
                room_id, token, _ = rng.choice(known)
                sock.push(join_msg(room_id, token=token))
            elif op < 0.5:
                sock.push(json.dumps({"type": "leave"}))
            elif op < 0.62:
                sock.hang_up()
            elif op < 0.82 and known:
                pad = "z" * rng.randrange(1, 3000)
                to = rng.choice(known)[2]
                sock.push(json.dumps({"type": "signal", "to": to, "data": {"p": pad}}))
                stats["signals"] += 1
            elif op < 0.86:
                sock.push(json.dumps({"type": "mute", "muted": rng.random() < 0.5}))
            else:
                fake_clock(state).advance(rng.uniform(0, 6))
                state.hub.sweep()
                state.limits.sweep()
        for _ in range(3):
            await asyncio.sleep(0)

    for sock, _task in live:
        sock.hang_up()
    await asyncio.wait_for(asyncio.gather(*every, return_exceptions=True), 5.0)
    fake_clock(state).advance(16)
    state.hub.sweep()
    state.limits.sweep()
    for sock in seen:
        stats["resumed"] += sum(1 for m in sock.sent if m["type"] == "joined" and m["resumed"])
        stats["replaced"] += sock.close_started == 4000
    stats["overflow"] = state.closed_by_limits  # only 4001 here: the other limits are huge
    assert state.hub.stats() == HubStats(0, 0, 0)
    assert state.limits.connections == 0
    assert [state.limits.seats(k) for k in keys] == [0, 0, 0]
    assert state.limits.queued_bytes == SENTINEL
    assert min(stats.values()) > 0, stats  # the run went through the interesting paths


async def _stuck_send(data: str) -> None:
    await asyncio.Event().wait()
