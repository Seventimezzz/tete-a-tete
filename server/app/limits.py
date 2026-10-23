"""Per-key and global limits. Pure logic: time comes from the injected clock.

Keys are derived from client IPs, so nothing here may log them or put them in
a repr or an exception.
"""

import ipaddress
from collections import deque
from dataclasses import dataclass
from typing import Final

from app.clock import Clock

UNKNOWN: Final = "unknown"


def ip_key(host: str | None) -> str:
    """IPv4 as is, IPv6 as its /64, ::ffff:a.b.c.d as IPv4, anything else as "unknown"."""
    if not host:
        return UNKNOWN
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return UNKNOWN
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(addr), 64), strict=False))
    return str(addr)


@dataclass(frozen=True)
class LimitConfig:
    seats_per_key: int = 20
    handshakes_per_window: int = 30
    joins_per_window: int = 30
    creates_per_window: int = 10
    window: float = 60.0
    msg_burst: int = 200
    msg_rate: float = 20.0
    bytes_burst: int = 262_144
    bytes_rate: float = 32_768.0
    key_bytes_burst: int = 524_288
    key_bytes_rate: float = 65_536.0
    max_connections: int = 1000
    max_rooms: int = 500
    max_queued_bytes: int = 134_217_728


class TokenBucket:
    def __init__(self, capacity: float, rate: float, now: float) -> None:
        self._capacity = capacity
        self._rate = rate
        self._tokens = capacity
        self._at = now

    def _refill(self, now: float) -> None:
        if now > self._at:
            self._tokens = min(self._capacity, self._tokens + (now - self._at) * self._rate)
            self._at = now

    def take(self, amount: float, now: float) -> bool:
        self._refill(now)
        if self._tokens < amount:
            return False
        self._tokens -= amount
        return True

    def is_full(self, now: float) -> bool:
        self._refill(now)
        return self._tokens >= self._capacity


type Window = dict[str, deque[float]]


class Limits:
    def __init__(self, config: LimitConfig, clock: Clock) -> None:
        self.config = config
        self._clock = clock
        self._connections = 0
        self._queued = 0
        self._seats: dict[str, int] = {}
        self._handshakes: Window = {}
        self._joins: Window = {}
        self._creates: Window = {}
        self._key_bytes: dict[str, TokenBucket] = {}

    def __repr__(self) -> str:
        return f"Limits(connections={self._connections}, queued_bytes={self._queued})"

    @property
    def connections(self) -> int:
        return self._connections

    @property
    def queued_bytes(self) -> int:
        return self._queued

    def seats(self, key: str) -> int:
        return self._seats.get(key, 0)

    def _hits(self, table: Window, key: str, now: float, maxlen: int | None = None) -> deque[float]:
        """Timestamps for key within the window, expired ones dropped."""
        hits = table.get(key)
        if hits is None:
            hits = table[key] = deque(maxlen=maxlen)
        while hits and hits[0] <= now - self.config.window:
            hits.popleft()
        return hits

    def try_open(self, key: str) -> bool:
        now = self._clock.now()
        hits = self._hits(self._handshakes, key, now, self.config.handshakes_per_window + 1)
        hits.append(now)  # every attempt counts, refused ones too
        if len(hits) > self.config.handshakes_per_window:
            return False
        if self._seats.get(key, 0) >= self.config.seats_per_key:
            return False
        if self._connections >= self.config.max_connections:
            return False
        self._connections += 1
        self._seats[key] = self._seats.get(key, 0) + 1
        return True

    def connection_closed(self) -> None:
        self._connections = max(0, self._connections - 1)

    def release_seat(self, key: str) -> None:
        left = self._seats.get(key, 0) - 1
        if left > 0:
            self._seats[key] = left
        else:
            self._seats.pop(key, None)

    def _try_window(self, table: Window, key: str, limit: int) -> bool:
        now = self._clock.now()
        hits = self._hits(table, key, now)
        if len(hits) >= limit:
            return False  # refused attempts are not recorded
        hits.append(now)
        return True

    def try_join(self, key: str) -> bool:
        return self._try_window(self._joins, key, self.config.joins_per_window)

    def try_create(self, key: str) -> bool:
        return self._try_window(self._creates, key, self.config.creates_per_window)

    def take_key_bytes(self, key: str, n: int) -> bool:
        now = self._clock.now()
        bucket = self._key_bytes.get(key)
        if bucket is None:
            bucket = TokenBucket(self.config.key_bytes_burst, self.config.key_bytes_rate, now)
            self._key_bytes[key] = bucket
        return bucket.take(n, now)

    def reserve(self, n: int) -> bool:
        if self._queued + n > self.config.max_queued_bytes:
            return False
        self._queued += n
        return True

    def free(self, n: int) -> None:
        self._queued = max(0, self._queued - n)

    def sweep(self) -> None:
        """Drop expired windows and full byte buckets of keys without seats."""
        now = self._clock.now()
        for table in (self._handshakes, self._joins, self._creates):
            for key in list(table):
                if not self._hits(table, key, now):
                    del table[key]
        for key in list(self._key_bytes):
            if key not in self._seats and self._key_bytes[key].is_full(now):
                del self._key_bytes[key]


class OutboxLedger[T]:
    """Bytes in the outgoing queues of connections, grouped by key: who holds the most.

    Owners are compared by identity. An owner whose queue is empty and a key without owners
    are forgotten at once, so no key outlives the queues of its connections.
    """

    def __init__(self) -> None:
        self._owners: dict[str, dict[T, int]] = {}
        self._totals: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"OutboxLedger(keys={len(self._owners)})"

    def change(self, key: str, owner: T, delta: int) -> None:
        owners = self._owners.setdefault(key, {})
        held = owners.get(owner, 0) + delta
        if held > 0:
            owners[owner] = held
        else:
            owners.pop(owner, None)
        total = self._totals.get(key, 0) + delta
        if owners and total > 0:
            self._totals[key] = total
        else:
            self._owners.pop(key, None)
            self._totals.pop(key, None)

    def key_bytes(self, key: str) -> int:
        return self._totals.get(key, 0)

    def largest(self, key: str | None = None) -> tuple[T, int] | None:
        """The owner with the most bytes, within one key or over all keys."""
        if key is None:
            groups = list(self._owners.values())
        else:
            groups = [self._owners[key]] if key in self._owners else []
        best: tuple[T, int] | None = None
        for owners in groups:
            for owner, held in owners.items():
                if best is None or held > best[1]:
                    best = (owner, held)
        return best
