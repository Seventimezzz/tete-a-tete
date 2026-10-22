"""Test doubles shared across test modules."""

from typing import Final

START: Final = 1000.0


class FakeClock:
    """Manually advanced clock for tests without sleeping."""

    def __init__(self) -> None:
        self._now = START

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
