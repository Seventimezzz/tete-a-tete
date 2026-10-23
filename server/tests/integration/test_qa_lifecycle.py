"""QA, stage 1: room life cycle over a real uvicorn, with the clock moved by the test.

The 15 s wait runs through the lifespan sweeper of the real process: the test jumps the server
clock instead of waiting, and only the sweeper's 1 s tick passes in real time.
"""

import asyncio
import json
from collections.abc import Iterator, Mapping
from typing import Any, Protocol

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from tests.integration.conftest import assert_clean_log
from tests.integration.harness import TEST_SECRET, Client, RunningServer, start_server

pytestmark = pytest.mark.integration

QA_FACTORY = "tests.integration.qa_factory:make_app"
SWEEP_TICK_BOUND = 3.0  # the sweeper runs once a second; generous for CI


class QaStart(Protocol):
    def __call__(self, *, limits: Mapping[str, Any] | None = None) -> RunningServer: ...


@pytest.fixture
def qa_start() -> Iterator[QaStart]:
    started: list[RunningServer] = []

    def _start(*, limits: Mapping[str, Any] | None = None) -> RunningServer:
        env = {"RESUME_SECRET": TEST_SECRET}
        if limits is not None:
            env["TETE_TEST_LIMITS"] = json.dumps(limits)
        server = start_server(app=QA_FACTORY, factory=True, env=env)
        started.append(server)
        return server

    yield _start
    for server in started:
        assert_clean_log(server)


class ServerClock:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    @classmethod
    async def open(cls, server: RunningServer) -> "ServerClock":
        return cls(await connect(f"ws://127.0.0.1:{server.port}/qa/clock", open_timeout=5))

    async def advance(self, seconds: float) -> None:
        await self._ws.send(str(seconds))
        await asyncio.wait_for(self._ws.recv(), 2.0)

    async def close(self) -> None:
        await self._ws.close()


async def refused(server: RunningServer, forwarded_for: str) -> bool:
    """The handshake from this address gets 403."""
    try:
        client = await Client.connect(server, forwarded_for=forwarded_for)
    except InvalidStatus as exc:
        status: int = exc.response.status_code
        return status == 403
    await client.close()
    return False


async def test_waiting_place_expires_through_the_lifespan_sweeper(qa_start: QaStart) -> None:
    """Drop, 14.9 s (place kept, still a seat in peers), 15.1 s (peer-left from the sweeper,
    the IP slot of the waiting place is free), return with the token gets resumed: false."""
    server = qa_start(limits={"seats_per_key": 1})
    clock = await ServerClock.open(server)
    a = await Client.connect(server, forwarded_for="198.51.100.61")
    b = await Client.connect(server, forwarded_for="198.51.100.62")
    joined_a = await a.create("Аня-уборка")
    room_id = joined_a["roomId"]
    joined_b = await b.join(room_id, "Борис-уборка")
    await a.expect("peer-joined")

    await b.close()
    await a.ping()
    await a.ping()  # two round trips: the server has handled b's close by now
    assert await refused(server, "198.51.100.62")  # the waiting place holds b's slot

    await clock.advance(14.9)
    c = await Client.connect(server, forwarded_for="198.51.100.63")
    joined_c = await c.join(room_id, "Вика-уборка")
    assert {p["id"] for p in joined_c["peers"]} == {joined_a["selfId"], joined_b["selfId"]}
    assert (await a.expect("peer-joined"))["id"] == joined_c["selfId"]
    await a.ping()  # no peer-left before the pong

    await clock.advance(0.2)
    left = {"type": "peer-left", "id": joined_b["selfId"]}
    assert await a.expect("peer-left", SWEEP_TICK_BOUND) == left
    assert await c.expect("peer-left", SWEEP_TICK_BOUND) == left

    b2 = await Client.connect(server, forwarded_for="198.51.100.62")  # the slot is free
    back = await b2.join(room_id, "Борис-уборка", joined_b["resumeToken"])
    assert back["resumed"] is False
    assert back["selfId"] != joined_b["selfId"]
    assert (await a.expect("peer-joined"))["id"] == back["selfId"]
    for client in (a, b2, c):
        await client.close()
    await clock.close()


async def test_last_waiting_place_expires_and_the_link_still_works(qa_start: QaStart) -> None:
    server = qa_start()
    clock = await ServerClock.open(server)
    a = await Client.connect(server, forwarded_for="198.51.100.64")
    joined_a = await a.create("Аня-пусто")
    room_id = joined_a["roomId"]
    await a.close()
    probe = await Client.connect(server, forwarded_for="198.51.100.65")
    await probe.ping()
    await probe.ping()
    await clock.advance(15.0)  # the join below frees the stale place even before the sweep

    b = await Client.connect(server, forwarded_for="198.51.100.66")
    joined_b = await b.join(room_id, "Борис-пусто")
    assert (joined_b["roomId"], joined_b["resumed"], joined_b["peers"]) == (room_id, False, [])
    for client in (b, probe):
        await client.close()
    await clock.close()


async def test_create_window_reopens_after_60_seconds(qa_start: QaStart) -> None:
    server = qa_start(limits={"creates_per_window": 1})
    clock = await ServerClock.open(server)
    client = await Client.connect(server, forwarded_for="198.51.100.67")
    await client.create("Создатель-окно")
    await client.send_json({"type": "leave"})
    await clock.advance(59.9)
    await client.send_json({"type": "create", "name": "Создатель-окно", "net": _net()})
    assert await client.expect("error") == {"type": "error", "code": "rate-limited"}
    await clock.advance(0.2)
    await client.create("Создатель-окно")
    await client.close()
    await clock.close()


def _net() -> dict[str, Any]:
    return {"nat": None, "upnp": None, "cgnat": None, "doubleNat": None, "vpn": None}


async def test_two_joins_with_one_token_at_once_leave_one_owner(qa_start: QaStart) -> None:
    """R3 over the network: the old connection and the first new one get 4000, the last wins,
    the others hear nothing, and signals reach the winner."""
    server = qa_start()
    a = await Client.connect(server, forwarded_for="198.51.100.68")
    old = await Client.connect(server, forwarded_for="198.51.100.69")
    joined_a = await a.create("Аня-гонка")
    room_id = joined_a["roomId"]
    joined_b = await old.join(room_id, "Борис-гонка")
    await a.expect("peer-joined")

    n1 = await Client.connect(server, forwarded_for="198.51.100.69")
    n2 = await Client.connect(server, forwarded_for="198.51.100.69")
    message = {
        "type": "join",
        "roomId": room_id,
        "name": "Борис-гонка",
        "net": _net(),
        "resumeToken": joined_b["resumeToken"],
    }
    await asyncio.gather(n1.send_json(message), n2.send_json(message))
    first, second = await asyncio.gather(n1.expect("joined"), n2.expect("joined"))
    assert first["selfId"] == second["selfId"] == joined_b["selfId"]
    assert first["resumed"] is second["resumed"] is True

    codes = await asyncio.gather(old.close_code(), _code_or_open(n1), _code_or_open(n2))
    assert codes[0] == 4000
    assert sorted(codes[1:], key=str) == [4000, "open"]
    winner = n1 if codes[1] == "open" else n2
    await a.ping()  # nothing about the takeover reached a
    await a.signal(joined_b["selfId"], {"description": {"type": "offer", "sdp": "победитель"}})
    assert (await winner.expect("signal"))["from"] == joined_a["selfId"]
    for client in (a, n1, n2, old):
        await client.close()


async def _code_or_open(client: Client) -> int | str | None:
    try:
        return await client.close_code(timeout=1.5)
    except TimeoutError:
        return "open"


async def test_room_full_with_a_waiting_place_then_free(qa_start: QaStart) -> None:
    server = qa_start()
    clients = [await Client.connect(server, forwarded_for=f"198.51.100.{70 + i}") for i in range(5)]
    joined = [await clients[0].create("Полная-0")]
    room_id = joined[0]["roomId"]
    for i, client in enumerate(clients[1:], 1):
        joined.append(await client.join(room_id, f"Полная-{i}"))
    await clients[4].close()  # the place waits and still takes a seat
    for _ in range(2):  # two round trips: the server has handled the close by now
        await clients[0].send_json({"type": "ping"})
        while (await clients[0].recv_json())["type"] != "pong":
            pass

    sixth = await Client.connect(server, forwarded_for="198.51.100.80")
    await sixth.send_json({"type": "join", "roomId": room_id, "name": "Шестой", "net": _net()})
    assert await sixth.expect("error") == {"type": "error", "code": "room-full"}
    await sixth.ping()  # room-full does not close the connection

    back = await Client.connect(server, forwarded_for="198.51.100.74")
    again = await back.join(room_id, "Полная-4", joined[4]["resumeToken"])
    assert (again["resumed"], again["selfId"]) == (True, joined[4]["selfId"])
    await clients[1].send_json({"type": "leave"})
    joined_sixth = await sixth.join(room_id, "Шестой")
    assert len(joined_sixth["peers"]) == 4
    for client in (*clients[:4], back, sixth):
        await client.close()
