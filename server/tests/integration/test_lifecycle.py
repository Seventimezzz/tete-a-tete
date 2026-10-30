"""Room life cycle over a real uvicorn: enter, leave, drop and return, restart, shutdown."""

import asyncio
import itertools
import os
import signal
import struct
import sys
from collections.abc import Coroutine
from typing import Any

import pytest

from tests.integration.conftest import StartServer
from tests.integration.harness import RAISED_LIMITS, Client, RunningServer, marker

pytestmark = pytest.mark.integration

ABRUPT_CLIENTS = 50
ROOM_SIZE = 5


def offer(label: str) -> dict[str, object]:
    return {"description": {"type": "offer", "sdp": f"v=0 {marker(label)}"}}


async def test_enter_and_leave(server: RunningServer) -> None:
    a = await Client.connect(server)
    b = await Client.connect(server)
    joined_a = await a.create("Аня-вход")
    assert joined_a["resumed"] is False
    assert joined_a["peers"] == []
    room_id = joined_a["roomId"]

    joined_b = await b.join(room_id, "Борис-вход")
    assert joined_b["roomId"] == room_id
    assert joined_b["resumed"] is False
    assert joined_b["peers"] == [{"id": joined_a["selfId"], "name": "Аня-вход", "muted": False}]
    peer_joined = await a.expect("peer-joined")
    assert peer_joined == {"type": "peer-joined", "id": joined_b["selfId"], "name": "Борис-вход"}

    await b.send_json({"type": "leave"})
    assert await a.expect("peer-left") == {"type": "peer-left", "id": joined_b["selfId"]}
    await a.send_json({"type": "leave"})
    # Both connections stay open after leave.
    await a.ping()
    await b.ping()
    await a.close()
    await b.close()


async def test_drop_and_return_with_queue(server: RunningServer) -> None:
    a = await Client.connect(server)
    b = await Client.connect(server)
    joined_a = await a.create("Аня-обрыв")
    room_id = joined_a["roomId"]
    joined_b = await b.join(room_id, "Борис-обрыв")
    await a.expect("peer-joined")

    b.abort()
    await a.settle()  # the server has noticed the drop: b's place waits
    first, second = offer("очередь-1"), offer("очередь-2")
    await a.signal(joined_b["selfId"], first)
    await a.signal(joined_b["selfId"], second)

    b2 = await Client.connect(server)
    back = await b2.join(room_id, "другое-имя", joined_b["resumeToken"])
    assert back["resumed"] is True
    assert back["selfId"] == joined_b["selfId"]
    assert back["peers"] == [{"id": joined_a["selfId"], "name": "Аня-обрыв", "muted": False}]
    for data in (first, second):
        assert await b2.expect("signal") == {
            "type": "signal",
            "from": joined_a["selfId"],
            "data": data,
        }
    # The others hear nothing about the drop and the return.
    await a.ping()
    await a.close()
    await b2.close()


async def test_leave_then_token_gives_resumed_false(server: RunningServer) -> None:
    a = await Client.connect(server)
    b = await Client.connect(server)
    joined_a = await a.create("Аня-уход")
    room_id = joined_a["roomId"]
    joined_b = await b.join(room_id, "Борис-уход")
    await a.expect("peer-joined")

    await b.send_json({"type": "leave"})
    await a.expect("peer-left")
    again = await b.join(room_id, "Борис-уход", joined_b["resumeToken"])
    assert again["resumed"] is False
    assert again["selfId"] != joined_b["selfId"]
    assert (await a.expect("peer-joined"))["id"] == again["selfId"]
    await a.close()
    await b.close()


async def test_server_restart_everyone_resumes_with_same_ids(start: StartServer) -> None:
    first = start()
    a, b, c = [await Client.connect(first) for _ in range(3)]
    joined_a = await a.create("Аня-рестарт")
    room_id = joined_a["roomId"]
    joined_b = await b.join(room_id, "Борис-рестарт")
    joined_c = await c.join(room_id, "Галя-рестарт")
    first.kill()
    for client in (a, b, c):
        await client.close()

    second = await asyncio.to_thread(start)
    second.sensitive.update(first.sensitive)
    a2, b2, c2 = [await Client.connect(second) for _ in range(3)]
    back_a = await a2.join(room_id, "Аня-рестарт", joined_a["resumeToken"])
    assert (back_a["resumed"], back_a["selfId"], back_a["peers"]) == (
        True,
        joined_a["selfId"],
        [],
    )
    back_b = await b2.join(room_id, "Борис-рестарт", joined_b["resumeToken"])
    assert (back_b["resumed"], back_b["selfId"]) == (True, joined_b["selfId"])
    back_c = await c2.join(room_id, "Галя-рестарт", joined_c["resumeToken"])
    assert (back_c["resumed"], back_c["selfId"]) == (True, joined_c["selfId"])
    assert {p["id"] for p in back_c["peers"]} == {joined_a["selfId"], joined_b["selfId"]}

    assert (await a2.expect("peer-joined"))["id"] == joined_b["selfId"]
    assert (await a2.expect("peer-joined"))["id"] == joined_c["selfId"]
    assert (await b2.expect("peer-joined"))["id"] == joined_c["selfId"]
    for client in (a2, b2, c2):
        await client.close()


async def test_abc_scenario_from_design(server: RunningServer) -> None:
    a, b, c = [await Client.connect(server) for _ in range(3)]
    joined_a = await a.create("Аня")
    room_id = joined_a["roomId"]
    joined_b = await b.join(room_id, "Борис 🎧")
    assert (await a.expect("peer-joined"))["name"] == "Борис 🎧"
    joined_c = await c.join(room_id, "Галя")
    assert [p["name"] for p in joined_c["peers"]] == ["Аня", "Борис 🎧"]
    assert (await a.expect("peer-joined"))["id"] == joined_c["selfId"]
    assert (await b.expect("peer-joined"))["id"] == joined_c["selfId"]

    ids = {a: joined_a["selfId"], b: joined_b["selfId"], c: joined_c["selfId"]}
    for sender, receiver in itertools.permutations((a, b, c), 2):
        data = {"candidate": {"candidate": f"кандидат {marker('пара')}", "sdpMid": "0"}}
        await sender.signal(ids[receiver], data)
        assert await receiver.expect("signal") == {
            "type": "signal",
            "from": ids[sender],
            "data": data,
        }

    await b.send_json({"type": "leave"})
    assert await a.expect("peer-left") == {"type": "peer-left", "id": ids[b]}
    assert await c.expect("peer-left") == {"type": "peer-left", "id": ids[b]}
    for client in (a, b, c):
        await client.close()


async def test_half_open_old_connection_replaced_fast(server: RunningServer) -> None:
    a = await Client.connect(server)
    old = await Client.connect(server)
    joined_a = await a.create("Аня-полуоткрытое")
    room_id = joined_a["roomId"]
    joined_b = await old.join(room_id, "Борис-полуоткрытое")
    await a.expect("peer-joined")

    old.pause_reading()  # R1: the old connection is half-open and reads nothing
    new = await Client.connect(server)
    loop = asyncio.get_running_loop()
    started = loop.time()
    back = await new.join(room_id, "Борис-полуоткрытое", joined_b["resumeToken"])
    assert loop.time() - started < 1.0
    assert (back["resumed"], back["selfId"]) == (True, joined_b["selfId"])

    old.resume_reading()
    assert await old.close_code() == 4000
    await old.ws.wait_closed()
    # pong comes first: nobody got peer-left, neither at the takeover nor at the old close.
    await a.ping()
    await new.ping()
    for client in (a, new):
        await client.close()


def _partial_text_frame() -> bytes:
    """A masked text frame announcing 1000 bytes of payload but carrying 100."""
    return struct.pack("!BBH", 0x81, 0x80 | 126, 1000) + os.urandom(4) + b"x" * 100


async def _fill_room(server: RunningServer, first: int) -> list[tuple[Client, str]]:
    """Five clients, each with its own X-Forwarded-For, in one new room: (client, peer id)."""
    clients = [
        await Client.connect(server, forwarded_for=f"198.51.100.{first + i + 1}")
        for i in range(ROOM_SIZE)
    ]
    joined = [await clients[0].create(f"Обрыв-{first:02d}")]
    room_id = joined[0]["roomId"]
    for i, client in enumerate(clients[1:], 1):
        joined.append(await client.join(room_id, f"Обрыв-{first + i:02d}"))
    return [(client, j["selfId"]) for client, j in zip(clients, joined, strict=True)]


async def _drop_mid_send(client: Client, self_id: str, peer_ids: list[str]) -> None:
    """Signal everyone in the room, start one more frame and drop TCP in its middle."""
    for peer_id in peer_ids:
        if peer_id != self_id:
            await client.signal(peer_id, offer("обрыв"))
    client.ws.transport.write(_partial_text_frame())
    await asyncio.sleep(0.01)  # let the partial frame reach the socket
    client.abort()


async def test_abrupt_disconnects_leave_no_traceback(start: StartServer) -> None:
    server = start(limits=RAISED_LIMITS)
    rooms = await asyncio.gather(
        *(_fill_room(server, r * ROOM_SIZE) for r in range(ABRUPT_CLIENTS // ROOM_SIZE))
    )
    drops: list[Coroutine[Any, Any, None]] = []
    for room in rooms:
        peer_ids = [peer_id for _, peer_id in room]
        drops.extend(_drop_mid_send(client, peer_id, peer_ids) for client, peer_id in room)
    await asyncio.gather(*drops)
    probe = await Client.connect(server)
    await probe.settle()  # the log check must see what the drops caused
    await probe.close()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM")
async def test_graceful_shutdown_closes_with_1012(server: RunningServer) -> None:
    a = await Client.connect(server)
    await a.create("Аня-остановка")
    server.process.send_signal(signal.SIGTERM)
    assert await a.close_code() == 1012
    await a.close()
    server.process.wait(10)
