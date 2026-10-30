"""A real uvicorn process and WebSocket clients for integration tests.

Clients remember every room id, token, peer id, name and data marker they send or receive,
so that the server output can be checked for them afterwards.
"""

import asyncio
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any, Final, Self

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

SERVER_DIR: Final = Path(__file__).resolve().parents[2]
TEST_SECRET: Final = "integration-test-secret-0123456789abcdef"  # noqa: S105 - test value
NET: Final[dict[str, Any]] = {
    "nat": None,
    "upnp": None,
    "cgnat": None,
    "doubleNat": None,
    "vpn": None,
}
# Limits high enough for floods and many clients from one test.
RAISED_LIMITS: Final[dict[str, Any]] = {
    "seats_per_key": 1000,
    "handshakes_per_window": 1000,
    "joins_per_window": 1000,
    "creates_per_window": 1000,
    "msg_burst": 100_000,
    "msg_rate": 100_000.0,
    "bytes_burst": 1_000_000_000,
    "bytes_rate": 1_000_000_000.0,
    "key_bytes_burst": 1_000_000_000,
    "key_bytes_rate": 1_000_000_000.0,
}
READY_SECONDS: Final = 10.0
STOP_SECONDS: Final = 10.0
# Strings in signal data shorter than this are protocol words ("offer"), not markers.
MARKER_MIN_LENGTH: Final = 6
_TEST_ENV_PREFIX: Final = "TETE_TEST_"
_UVICORN_FLAGS: Final = (
    "--host",
    "127.0.0.1",
    "--workers",
    "1",
    "--ws-ping-interval",
    "15",
    "--ws-ping-timeout",
    "15",
    "--ws-max-size",
    "65536",
    "--proxy-headers",
    "--forwarded-allow-ips",
    "127.0.0.1",
    "--no-access-log",
    "--ws",
    "app.wsproto:TeteWebSocketProtocol",
)


async def stop_task(task: "asyncio.Task[Any]") -> None:
    """Cancel a helper task and wait until it ends, so that a failed or timed-out test leaves
    nothing running. Its result or error is taken here, so none is reported as unretrieved:
    a test that needs it awaits the task itself first."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def marker(label: str) -> str:
    """A unique string for signal data: the server output must never show it."""
    return f"маркер-{label}-{secrets.token_hex(4)}"


class ServerExitError(Exception):
    """The process exited before accepting connections; `output` is what it printed."""

    def __init__(self, returncode: int, output: str) -> None:
        super().__init__(f"uvicorn exited with {returncode} before accepting connections")
        self.returncode = returncode
        self.output = output


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class RunningServer:
    """A uvicorn process. `sensitive` collects strings that must never reach its output."""

    def __init__(self, process: subprocess.Popen[bytes], port: int, output: IO[bytes]) -> None:
        self.process = process
        self.port = port
        self.url = f"ws://127.0.0.1:{port}/ws"
        self.sensitive: set[str] = set()
        self._output_file = output
        self._output: str | None = None

    def kill(self) -> None:
        """Hard kill, as a crash would: no shutdown, no close frames."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(STOP_SECONDS)

    def stop(self) -> str:
        """Stop the process (SIGTERM on POSIX, a hard kill on Windows) and return its
        stdout and stderr. Idempotent: later calls return the same text."""
        if self._output is None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(STOP_SECONDS)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(STOP_SECONDS)
            self._output = _read_output(self._output_file)
        return self._output


def _read_output(output: IO[bytes]) -> str:
    output.seek(0)
    text = output.read().decode("utf-8", errors="replace")
    output.close()
    return text


def start_server(
    *,
    app: str = "app.main:app",
    factory: bool = False,
    env: Mapping[str, str] | None = None,
    ping_seconds: float | None = None,
) -> RunningServer:
    """Start `python -m uvicorn <app>` with the flags from CLAUDE.md on a free port and wait
    until the port accepts connections (uvicorn binds it after the lifespan startup).

    `ping_seconds` replaces the 15 s of --ws-ping-interval and --ws-ping-timeout, for tests
    of the keepalive; every other flag stays as in production."""
    port = free_port()
    flags = list(_UVICORN_FLAGS)
    if ping_seconds is not None:
        for flag in ("--ws-ping-interval", "--ws-ping-timeout"):
            flags[flags.index(flag) + 1] = str(ping_seconds)
    args = [sys.executable, "-m", "uvicorn", app, *flags, "--port", str(port)]
    if factory:
        args.append("--factory")
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(_TEST_ENV_PREFIX)}
    full_env.update({"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
    full_env.update(env or {})
    output = tempfile.TemporaryFile()  # noqa: SIM115 - closed by RunningServer
    process = subprocess.Popen(  # noqa: S603 - fixed arguments, our own interpreter
        args, cwd=SERVER_DIR, env=full_env, stdout=output, stderr=subprocess.STDOUT
    )
    deadline = time.monotonic() + READY_SECONDS
    while True:
        returncode = process.poll()
        if returncode is not None:
            raise ServerExitError(returncode, _read_output(output))
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
            return RunningServer(process, port, output)
        if time.monotonic() > deadline:
            process.kill()
            process.wait(STOP_SECONDS)
            raise ServerExitError(-1, _read_output(output))
        time.sleep(0.05)


def _remember(server: RunningServer, message: Mapping[str, Any]) -> None:
    """Record the access keys and personal data a message carries."""
    for field in ("roomId", "resumeToken", "name", "selfId", "id", "from", "to"):
        value = message.get(field)
        if isinstance(value, str) and value:
            server.sensitive.add(value)
    for peer in message.get("peers") or []:
        _remember(server, peer)
    if "data" in message:
        server.sensitive.update(_markers(message["data"]))


def _markers(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if len(value) >= MARKER_MIN_LENGTH else set()
    if isinstance(value, dict):
        return {m for item in value.values() for m in _markers(item)}
    if isinstance(value, list):
        return {m for item in value for m in _markers(item)}
    return set()


class Client:
    """A websockets client that speaks JSON and remembers sensitive values."""

    def __init__(self, ws: ClientConnection, server: RunningServer) -> None:
        self.ws = ws
        self.server = server

    @classmethod
    async def connect(
        cls,
        server: RunningServer,
        *,
        forwarded_for: str | None = None,
        path: str = "/ws",
        compression: bool = True,
        receive_buffer: int | None = None,
    ) -> Self:
        """`receive_buffer` sets SO_RCVBUF: a small one makes a paused reader fill up fast."""
        headers = {}
        if forwarded_for is not None:
            headers["X-Forwarded-For"] = forwarded_for
            server.sensitive.update(a.strip() for a in forwarded_for.split(","))
        url = f"ws://127.0.0.1:{server.port}{path}"
        sock = None
        if receive_buffer is not None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
            sock.setblocking(False)
            await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", server.port))
        ws = await connect(
            url,
            sock=sock,
            additional_headers=headers,
            compression="deflate" if compression else None,
            open_timeout=5,
            max_size=None,
        )
        return cls(ws, server)

    @property
    def deflate(self) -> bool:
        """permessage-deflate was negotiated."""
        return any(type(ext).__name__ == "PerMessageDeflate" for ext in self.ws.protocol.extensions)

    async def send_json(self, message: Mapping[str, Any]) -> None:
        _remember(self.server, message)
        await self.ws.send(json.dumps(message, ensure_ascii=False))

    async def recv_json(self, timeout: float = 2.0) -> dict[str, Any]:  # noqa: ASYNC109 - test helper
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        message: dict[str, Any] = json.loads(raw)
        _remember(self.server, message)
        return message

    async def expect(self, type_: str, timeout: float = 2.0) -> dict[str, Any]:  # noqa: ASYNC109 - test helper
        """The next message, which must be of this type."""
        message = await self.recv_json(timeout)
        assert message["type"] == type_, f"expected {type_}, got {message['type']}"
        return message

    async def create(self, name: str) -> dict[str, Any]:
        await self.send_json({"type": "create", "name": name, "net": NET})
        return await self.expect("joined")

    async def join(self, room_id: str, name: str, token: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"type": "join", "roomId": room_id, "name": name, "net": NET}
        if token is not None:
            message["resumeToken"] = token
        await self.send_json(message)
        return await self.expect("joined")

    async def signal(self, to: str, data: Mapping[str, Any]) -> None:
        await self.send_json({"type": "signal", "to": to, "data": data})

    async def ping(self, timeout: float = 2.0) -> float:  # noqa: ASYNC109 - test helper
        """Round trip of ping/pong in seconds."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.send_json({"type": "ping"})
        await self.expect("pong", timeout)
        return loop.time() - started

    async def settle(self) -> None:
        """Two ping round trips. No message tells that the server has noticed a dropped
        connection of another client; the drop reached the single event loop before this
        client's pings, so after two round trips the server has handled it."""
        await self.ping()
        await self.ping()

    async def close_code(self, timeout: float = 5.0) -> int | None:  # noqa: ASYNC109 - test helper
        """Read until the server closes the connection; the code of its close frame
        (None if the connection was dropped without one). Messages before it are skipped."""
        async with asyncio.timeout(timeout):
            while True:
                try:
                    await self.ws.recv()
                except ConnectionClosed as exc:
                    return exc.rcvd.code if exc.rcvd is not None else None

    def pause_reading(self) -> None:
        """Stop reading the socket: the server's frames pile up in the TCP buffers."""
        self.ws.transport.pause_reading()

    def resume_reading(self) -> None:
        self.ws.transport.resume_reading()

    def abort(self) -> None:
        """Drop the TCP connection without a close frame."""
        self.ws.transport.abort()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(), 2.0)
