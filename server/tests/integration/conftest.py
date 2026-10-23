"""Fixtures that start uvicorn and check its whole output when the test ends."""

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any, Final, Protocol

import pytest

from tests.integration.harness import TEST_SECRET, RunningServer, start_server

FACTORY: Final = "tests.integration.factory:make_app"
LOOPBACK: Final = "127.0.0.1"
# The one line where uvicorn prints its own address (R3): it is not a client IP.
_LISTEN_LINE: Final = re.compile(r"Uvicorn running on http://127\.0\.0\.1:\d+")


class StartServer(Protocol):
    def __call__(
        self, *, limits: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
    ) -> RunningServer: ...


def assert_clean_log(server: RunningServer) -> None:
    """The output holds no client IP, room id, token, peer id, name, data marker or traceback."""
    output = server.stop()
    leaks = sorted(s for s in server.sensitive if s in output)
    assert not leaks, f"server output leaks {len(leaks)} remembered value(s)"
    assert LOOPBACK not in _LISTEN_LINE.sub("", output), "server output holds 127.0.0.1"
    assert "Traceback" not in output, "server output holds a traceback"


@pytest.fixture
def start() -> Iterator[StartServer]:
    """Start servers with the test secret: `limits` runs the test factory with LimitConfig
    overrides, `env` adds variables (TETE_TEST_FAULT also needs the factory). Every server is
    stopped and its log checked when the test ends."""
    started: list[RunningServer] = []

    def _start(
        *, limits: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
    ) -> RunningServer:
        full_env = {"RESUME_SECRET": TEST_SECRET, **(env or {})}
        use_factory = limits is not None or "TETE_TEST_FAULT" in full_env
        if limits is not None:
            full_env["TETE_TEST_LIMITS"] = json.dumps(limits)
        server = start_server(
            app=FACTORY if use_factory else "app.main:app", factory=use_factory, env=full_env
        )
        started.append(server)
        return server

    yield _start
    for server in started:
        server.stop()
    for server in started:
        assert_clean_log(server)


@pytest.fixture
def server(start: StartServer) -> RunningServer:
    """`app.main:app` as in production, with the test RESUME_SECRET."""
    return start()
