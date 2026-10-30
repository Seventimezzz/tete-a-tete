"""Swappable clock: the only place that reads real time."""

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()
