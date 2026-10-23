"""Limits keyed by X-Forwarded-For, frame size limits and slow readers over a real uvicorn."""

import asyncio
import contextlib
import json

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from tests.integration.conftest import StartServer
from tests.integration.harness import (
    NET,
    RAISED_LIMITS,
    Client,
    RunningServer,
    ServerExitError,
    marker,
    start_server,
)

pytestmark = pytest.mark.integration

OVERSIZED = 70_000
FLOOD_ELEMENTS = 32_000  # a signal of about 64 KB made of small elements
SLOW_READER_PAYLOAD = 60_000
# About 12 MB: the TCP buffers between the paused reader and the server plus the 1 MB
# outgoing queue. On Windows about 30 messages are enough; Linux buffers grow larger.
SLOW_READER_MESSAGES = 200
SMALL_RECEIVE_BUFFER = 16_384


async def assert_refused(server: RunningServer, forwarded_for: str) -> None:
    with pytest.raises(InvalidStatus) as refused:
        await Client.connect(server, forwarded_for=forwarded_for)
    assert refused.value.response.status_code == 403


async def test_seat_limit_by_forwarded_for(start: StartServer) -> None:
    server = start(limits={"seats_per_key": 2})
    held = [await Client.connect(server, forwarded_for="198.51.100.1") for _ in range(2)]
    await assert_refused(server, "198.51.100.1")
    other = await Client.connect(server, forwarded_for="198.51.100.2")
    await other.ping()
    # Closing a connection frees its seat.
    await held.pop().close()
    again = await Client.connect(server, forwarded_for="198.51.100.1")
    await again.ping()
    for client in (*held, other, again):
        await client.close()


async def test_handshake_limit_by_forwarded_for(start: StartServer) -> None:
    server = start(limits={"handshakes_per_window": 2})
    for _ in range(2):
        await (await Client.connect(server, forwarded_for="198.51.100.3")).close()
    await assert_refused(server, "198.51.100.3")
    other = await Client.connect(server, forwarded_for="198.51.100.4")
    await other.ping()
    await other.close()


async def test_rightmost_forwarded_for_is_the_key(start: StartServer) -> None:
    server = start(limits={"seats_per_key": 1})
    chained = await Client.connect(server, forwarded_for="6.6.6.6, 7.7.7.7")
    await assert_refused(server, "7.7.7.7")
    leftmost = await Client.connect(server, forwarded_for="6.6.6.6")
    await leftmost.ping()
    for client in (chained, leftmost):
        await client.close()


async def test_create_rate_limited(start: StartServer) -> None:
    server = start(limits={"creates_per_window": 2})
    client = await Client.connect(server, forwarded_for="198.51.100.5")
    for i in range(2):
        await client.create(f"Создатель-{i}")
        await client.send_json({"type": "leave"})
    await client.send_json({"type": "create", "name": "Создатель-лишний", "net": NET})
    assert await client.expect("error") == {"type": "error", "code": "rate-limited"}
    # Another key creates freely.
    other = await Client.connect(server, forwarded_for="198.51.100.6")
    await other.create("Создатель-другой")
    for c in (client, other):
        await c.close()


async def test_oversized_message_closed_1009(server: RunningServer) -> None:
    whole = await Client.connect(server, compression=False)
    await whole.ws.send("x" * OVERSIZED)
    assert await whole.close_code() == 1009

    fragmented = await Client.connect(server, compression=False)
    await fragmented.ws.send(["x" * 10_000 for _ in range(OVERSIZED // 10_000)])
    assert await fragmented.close_code() == 1009

    compressed = await Client.connect(server, compression=True)
    assert compressed.deflate
    await compressed.ws.send("x" * OVERSIZED)  # deflates to a few hundred bytes on the wire
    assert await compressed.close_code() == 1009

    probe = await Client.connect(server)
    await probe.ping()
    await probe.close()


async def test_message_flood_closed_1008_others_still_fast(server: RunningServer) -> None:
    flooder = await Client.connect(server, forwarded_for="203.0.113.10", compression=False)
    target = await Client.connect(server, forwarded_for="203.0.113.11")
    other = await Client.connect(server, forwarded_for="203.0.113.12")
    room_id = (await flooder.create("Флудер"))["roomId"]
    target_id = (await target.join(room_id, "Цель"))["selfId"]
    await flooder.expect("peer-joined")

    big = {"type": "signal", "to": target_id, "data": {"candidate": [0] * FLOOD_ELEMENTS}}
    text = json.dumps(big, separators=(",", ":"))
    assert len(text) < 65_536
    sent = 0

    async def flood() -> None:
        nonlocal sent
        with contextlib.suppress(ConnectionClosed):
            while True:
                await flooder.ws.send(text)
                sent += 1
                await asyncio.sleep(0)

    flooding = asyncio.create_task(flood())
    latencies = []
    while not flooding.done():
        latencies.append(await other.ping())
    assert await flooder.close_code() == 1008
    assert latencies
    assert max(latencies) < 0.2, f"pong took {max(latencies):.3f} s"
    assert sent > 1
    for client in (target, other):
        await client.close()


async def test_slow_reader_closed_4001(start: StartServer) -> None:
    server = start(limits=RAISED_LIMITS)
    sender = await Client.connect(server, forwarded_for="203.0.113.20", compression=False)
    reader = await Client.connect(
        server,
        forwarded_for="203.0.113.21",
        compression=False,
        receive_buffer=SMALL_RECEIVE_BUFFER,
    )
    room_id = (await sender.create("Отправитель"))["roomId"]
    reader_id = (await reader.join(room_id, "Нечитающий"))["selfId"]
    await sender.expect("peer-joined")

    reader.pause_reading()
    data = {"description": {"type": "offer", "sdp": "x" * SLOW_READER_PAYLOAD + marker("4001")}}
    for _ in range(SLOW_READER_MESSAGES):
        await sender.signal(reader_id, data)
    # The server waits at most 1 s for the close frame to get out: read again promptly.
    reader.resume_reading()
    assert await reader.close_code(timeout=10.0) == 4001
    await sender.ping()
    await sender.close()


async def test_short_secret_fails_without_printing_it() -> None:
    secret = "short-secret-марк"  # noqa: S105 - test value
    try:
        server = start_server(env={"RESUME_SECRET": secret})
    except ServerExitError as exc:
        exited = exc
    else:
        server.stop()
        pytest.fail("uvicorn started with a short RESUME_SECRET")
    assert exited.returncode != 0
    assert "RESUME_SECRET" in exited.output
    assert secret not in exited.output
    assert "Traceback" not in exited.output
