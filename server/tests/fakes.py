"""Test doubles shared across test modules."""

import json
from typing import Any, Final

START: Final = 1000.0


class FakeClock:
    """Manually advanced clock for tests without sleeping."""

    def __init__(self) -> None:
        self._now = START

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeConn:
    """Connection double: records parsed outgoing messages and the close code."""

    def __init__(self, key: str = "k") -> None:
        self.key = key
        self.sent: list[dict[str, Any]] = []
        self.closed: int | None = None

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def close(self, code: int) -> None:
        self.closed = code

    def of_type(self, t: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == t]
