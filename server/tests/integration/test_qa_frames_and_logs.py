"""QA, stage 1: frame boundaries, IPv6 keys, malformed handshakes and what stays of a closed
connection, over a real uvicorn."""

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import time

import pytest
from websockets.exceptions import InvalidStatus

from tests.integration.conftest import StartServer
from tests.integration.harness import RAISED_LIMITS, Client, RunningServer, marker

pytestmark = pytest.mark.integration

MAX_FRAME = 65_536
SMALL_RECEIVE_BUFFER = 16_384
FLOOD_MESSAGES = 200
FLOOD_PAYLOAD = 60_000
# The server waits CLOSE_WAIT_SECONDS (1 s) for the close frame, then gives up; give it 5 s.
TEARDOWN_BOUND = 5.0


async def assert_refused(server: RunningServer, forwarded_for: str) -> None:
    with pytest.raises(InvalidStatus) as refused:
        await Client.connect(server, forwarded_for=forwarded_for)
    assert refused.value.response.status_code == 403


def _signal_text(to: str, total: int) -> str:
    """A signal frame of exactly `total` bytes (ASCII)."""
    head = json.dumps({"type": "signal", "to": to, "data": {"sdp": ""}}, separators=(",", ":"))
    pad = total - len(head)
    return head.replace('"sdp":""', '"sdp":"' + "s" * pad + '"')


async def test_frame_of_exactly_64k_passes_one_byte_more_closes_1009(
    server: RunningServer,
) -> None:
    a = await Client.connect(server, compression=False)
    b = await Client.connect(server, compression=False)
    room_id = (await a.create("Аня-кадр"))["roomId"]
    b_id = (await b.join(room_id, "Борис-кадр"))["selfId"]
    await a.expect("peer-joined")

    exact = _signal_text(b_id, MAX_FRAME)
    assert len(exact.encode()) == MAX_FRAME
    await a.ws.send(exact)
    got = await b.expect("signal")
    assert len(got["data"]["sdp"]) == len(json.loads(exact)["data"]["sdp"])

    await a.ws.send(_signal_text(b_id, MAX_FRAME + 1))
    assert await a.close_code() == 1009
    await b.ping()
    await b.close()


async def test_invalid_utf8_text_closes_1007_and_server_lives(server: RunningServer) -> None:
    client = await Client.connect(server, compression=False)
    await client.ws.send(b'{"type":"ping","x":"\xff\xfe"}', text=True)
    assert await client.close_code() == 1007
    probe = await Client.connect(server)
    await probe.ping()
    await probe.close()


async def test_ipv6_forwarded_for_is_keyed_by_its_64(start: StartServer) -> None:
    server = start(limits={"seats_per_key": 1})
    held = await Client.connect(server, forwarded_for="2001:db8:aa:bb::1")
    await assert_refused(server, "2001:db8:aa:bb:ffff:eeee:dddd:2")  # same /64
    other = await Client.connect(server, forwarded_for="2001:db8:aa:bc::1")  # next /64
    await other.ping()
    v4 = await Client.connect(server, forwarded_for="::ffff:198.51.100.40")
    await assert_refused(server, "198.51.100.40")  # IPv4-mapped is the IPv4 key
    for client in (held, other, v4):
        await client.close()


def _raw_request(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), 2) as sock:
        sock.sendall(request)
        sock.settimeout(2)
        with contextlib.suppress(OSError):
            return sock.recv(64)
    return b""


async def test_malformed_handshakes_and_plain_http_log_nothing_personal(
    server: RunningServer,
) -> None:
    room = "kmnpqrstuvwx"
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    upgrade = b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
    cases = {
        "203.0.113.141": b"Sec-WebSocket-Version: 99\r\nSec-WebSocket-Key: " + key.encode(),
        "203.0.113.142": b"Sec-WebSocket-Version: 13",
        "203.0.113.143": b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: short",
    }
    for ip, extra in cases.items():
        server.sensitive.add(ip)
        request = (
            f"GET /ws?r={room} HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: {ip}\r\n".encode()
            + upgrade
            + extra
            + b"\r\n\r\n"
        )
        assert (await asyncio.to_thread(_raw_request, server.port, request)).startswith(
            b"HTTP/1.1 400"
        )
    plain = {
        "203.0.113.144": f"GET /ws?r={room} HTTP/1.1\r\nHost: x\r\n",
        "203.0.113.145": f"GET /?r={room} HTTP/1.1\r\nHost: x\r\n",
        "203.0.113.146": "POST /ws HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n",
    }
    for ip, head in plain.items():
        server.sensitive.add(ip)
        request = f"{head}X-Forwarded-For: {ip}\r\n\r\nok".encode()
        assert (await asyncio.to_thread(_raw_request, server.port, request)).startswith(
            b"HTTP/1.1 4"
        )
    server.sensitive.update({room, "203.0.113.147"})
    garbage = b"GET /ws?r=" + room.encode() + b" 203.0.113.147\x00\x01 HTTP/9\r\n\r\n"
    await asyncio.to_thread(_raw_request, server.port, garbage)
    probe = await Client.connect(server)
    await probe.ping()
    await probe.close()
    # assert_clean_log in the fixture checks the output for every remembered value


class TransportLingeredError(AssertionError):
    """The server side of a connection it has given up on is still established."""


def _server_side_states(server_port: int, client_port: int) -> list[str]:
    """TCP states of the server's end of one connection, from the OS tables."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],  # noqa: S607 - system tool, fixed arguments
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        local, remote = f"127.0.0.1:{server_port}", f"127.0.0.1:{client_port}"
        return [
            parts[3]
            for parts in (line.split() for line in out.splitlines())
            if len(parts) >= 4 and parts[0] == "TCP" and parts[1:3] == [local, remote]
        ]
    if sys.platform.startswith("linux"):
        names = {"01": "ESTABLISHED", "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT"}
        states = []
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            with open(table, encoding="ascii") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    local_port = int(parts[1].rsplit(":", 1)[1], 16)
                    remote_port = int(parts[2].rsplit(":", 1)[1], 16)
                    if (local_port, remote_port) == (server_port, client_port):
                        states.append(names.get(parts[3], parts[3]))
        return states
    pytest.skip("no TCP table reader for this platform")


async def test_closed_slow_reader_transport_is_torn_down(start: StartServer) -> None:
    server = start(limits=RAISED_LIMITS)
    # The sender compresses: its 12 MB of "x" stay far under the raw budget of a connection.
    sender = await Client.connect(server, forwarded_for="203.0.113.150", compression=True)
    reader = await Client.connect(
        server,
        forwarded_for="203.0.113.151",
        compression=False,
        receive_buffer=SMALL_RECEIVE_BUFFER,
    )
    try:
        room_id = (await sender.create("Отправитель-обрыв"))["roomId"]
        reader_id = (await reader.join(room_id, "Нечитающий-обрыв"))["selfId"]
        await sender.expect("peer-joined")
        reader_port = reader.ws.transport.get_extra_info("sockname")[1]
        assert "ESTABLISHED" in _server_side_states(server.port, reader_port)

        reader.pause_reading()
        data = {"description": {"type": "offer", "sdp": "x" * FLOOD_PAYLOAD + marker("линг")}}
        for _ in range(FLOOD_MESSAGES):
            await sender.signal(reader_id, data)
        await sender.ping(timeout=10)  # every signal has been handled: the outbox overflowed

        deadline = time.monotonic() + TEARDOWN_BOUND
        while True:
            states = await asyncio.to_thread(_server_side_states, server.port, reader_port)
            if "ESTABLISHED" not in states or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        # The server really gave up on the reader: once it reads, the connection ends.
        reader.resume_reading()
        await reader.close_code(timeout=15.0)
        if "ESTABLISHED" in states:
            raise TransportLingeredError(
                f"server side still ESTABLISHED {TEARDOWN_BOUND} s after giving up"
            )
    finally:
        reader.resume_reading()
        for client in (sender, reader):
            await client.close()
