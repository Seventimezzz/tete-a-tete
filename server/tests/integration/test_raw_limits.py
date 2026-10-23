"""H1 over a real uvicorn with `--ws app.wsproto:TeteWebSocketProtocol`: control frames and
fragments are counted, a frozen client is dropped after the keepalive timeout."""

import asyncio
import json
import os
import struct
import time

import pytest

from tests.integration.conftest import assert_clean_log
from tests.integration.harness import RAISED_LIMITS, TEST_SECRET, Client, RunningServer, marker
from tests.integration.harness import start_server as start_uvicorn
from tests.integration.test_qa_frames_and_logs import _server_side_states
from tests.integration.test_qa_lifecycle import QA_FACTORY, SWEEP_TICK_BOUND, ServerClock

pytestmark = pytest.mark.integration

FLOOD_BOUND = 30.0  # a regression fails instead of hanging
CUT_BOUND = 5.0  # the raw budget allows 1024 frames at once: a flood is cut well within this
PONG_BOUND = 1.0  # wall clock on a shared CI runner
PING_PACE = 0.1
BATCH = 256
APP_BURST = 200
APP_RATE_SECONDS = 3.0
KEEPALIVE_SECONDS = 1.0
FROZEN_SIGNALS = 30  # about 1.8 MB: fills the TCP buffers, stays under the 2 MB outbox
FROZEN_PAYLOAD = 60_000
SMALL_RECEIVE_BUFFER = 16_384


def masked(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """A client frame (payload < 126 bytes) as it goes on the wire."""
    key = os.urandom(4)
    body = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return struct.pack("!BB", (0x80 if fin else 0) | opcode, 0x80 | len(payload)) + key + body


async def flood_until_cut(client: Client, first: bytes, chunk: bytes) -> float:
    """Write `first`, then `chunk` until the server drops the connection; seconds it took."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    client.ws.transport.write(first)
    async with asyncio.timeout(FLOOD_BOUND):
        while not client.ws.transport.is_closing():
            client.ws.transport.write(chunk)
            await asyncio.sleep(0.001)
    return loop.time() - started


async def pong_latencies_while(client: Client, flooding: "asyncio.Task[float]") -> list[float]:
    """Ping/pong round trips of a bystander, paced under its own message rate (20 a second)."""
    latencies = []
    async with asyncio.timeout(FLOOD_BOUND):
        while not flooding.done():
            latencies.append(await client.ping())
            await asyncio.sleep(PING_PACE)
    return latencies


async def test_ping_flood_is_cut_and_others_keep_their_pongs(server: RunningServer) -> None:
    flooder = await Client.connect(server, forwarded_for="203.0.113.40", compression=False)
    other = await Client.connect(server, forwarded_for="203.0.113.41")
    await flooder.create("Флудер-пинг")
    await other.create("Другая-комната")
    ping = masked(0x9, b"")
    flooding = asyncio.create_task(flood_until_cut(flooder, b"", ping * BATCH))
    latencies = await pong_latencies_while(other, flooding)
    assert await flooding < CUT_BOUND
    assert latencies
    assert max(latencies) < PONG_BOUND, f"pong took {max(latencies):.3f} s"
    assert await flooder.close_code() is None  # aborted, no close frame
    await other.ping()
    await other.close()


async def test_one_byte_fragment_flood_is_cut(server: RunningServer) -> None:
    flooder = await Client.connect(server, forwarded_for="203.0.113.42", compression=False)
    other = await Client.connect(server, forwarded_for="203.0.113.43")
    first = masked(0x1, b"{", fin=False)
    fragments = masked(0x0, b" ", fin=False) * BATCH
    flooding = asyncio.create_task(flood_until_cut(flooder, first, fragments))
    latencies = await pong_latencies_while(other, flooding)
    assert await flooding < CUT_BOUND
    assert max(latencies) < PONG_BOUND, f"pong took {max(latencies):.3f} s"
    assert await flooder.close_code() is None
    await other.ping()
    await other.close()


async def test_traffic_at_the_application_limits_is_cut_by_the_application(
    server: RunningServer,
) -> None:
    """The whole message burst, then 20 messages a second: the raw layer lets it all through.
    Past the burst the application closes with 1008, a close frame the raw layer never sends."""
    client = await Client.connect(server, forwarded_for="203.0.113.44", compression=False)
    ping = json.dumps({"type": "ping"})
    for _ in range(APP_BURST):
        await client.ws.send(ping)
    for _ in range(APP_BURST):
        await client.expect("pong")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + APP_RATE_SECONDS
    while loop.time() < deadline:
        await asyncio.sleep(0.05)
        await client.ping()
    for _ in range(APP_BURST // 4):
        await client.ws.send(ping)
    assert await client.close_code() == 1008


async def test_frozen_client_is_dropped_after_the_keepalive_timeout() -> None:
    """A client that neither reads nor answers pings, with the server's write buffer full:
    the transport is aborted after the ping timeout, the place waits, peer-left follows."""
    server = start_uvicorn(
        app=QA_FACTORY,
        factory=True,
        env={"RESUME_SECRET": TEST_SECRET, "TETE_TEST_LIMITS": json.dumps(RAISED_LIMITS)},
        ping_seconds=KEEPALIVE_SECONDS,
    )
    try:
        await _frozen_client_scenario(server)
    finally:
        assert_clean_log(server)


async def _frozen_client_scenario(server: RunningServer) -> None:
    clock = await ServerClock.open(server)
    # The sender compresses: its signals stay far under the raw budget of its connection.
    sender = await Client.connect(server, forwarded_for="203.0.113.45", compression=True)
    frozen = await Client.connect(
        server,
        forwarded_for="203.0.113.46",
        compression=False,
        receive_buffer=SMALL_RECEIVE_BUFFER,
    )
    joined = await sender.create("Отправитель-замер")
    frozen_id = (await frozen.join(joined["roomId"], "Замёрзший"))["selfId"]
    await sender.expect("peer-joined")
    frozen_port = frozen.ws.transport.get_extra_info("sockname")[1]

    frozen.pause_reading()  # reads nothing, so it answers no ping either
    data = {"description": {"type": "offer", "sdp": "x" * FROZEN_PAYLOAD + marker("замер")}}
    for _ in range(FROZEN_SIGNALS):
        await sender.signal(frozen_id, data)
    await sender.ping(timeout=10)

    deadline = time.monotonic() + 4 * KEEPALIVE_SECONDS + CUT_BOUND
    while True:
        states = await asyncio.to_thread(_server_side_states, server.port, frozen_port)
        if "ESTABLISHED" not in states or time.monotonic() > deadline:
            break
        await asyncio.sleep(0.25)
    assert "ESTABLISHED" not in states, "the frozen client's transport is still open"

    await sender.settle()  # the application has seen the disconnect: the place waits
    await clock.advance(15.0)
    left = await sender.expect("peer-left", SWEEP_TICK_BOUND)
    assert left == {"type": "peer-left", "id": frozen_id}
    frozen.resume_reading()
    for client in (sender, frozen):
        await client.close()
    await clock.close()
