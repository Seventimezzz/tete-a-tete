from typing import Any

import pytest

from app.limits import LimitConfig, Limits, TokenBucket, ip_key
from tests.fakes import FakeClock

KEY = "203.0.113.7"


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::9", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        (None, "unknown"),
        ("testclient", "unknown"),
        ("", "unknown"),
    ],
)
def test_ip_key(host: str | None, key: str) -> None:
    assert ip_key(host) == key


def make(**kw: Any) -> tuple[Limits, FakeClock]:
    clock = FakeClock()
    return Limits(LimitConfig(**kw), clock), clock


def test_twenty_seats_per_key_then_refused() -> None:
    limits, clock = make()
    for i in range(20):
        assert limits.try_open(KEY)
        if i % 10 == 9:
            clock.advance(61)  # keep handshake window out of the way
    assert limits.seats(KEY) == 20
    assert not limits.try_open(KEY)
    assert limits.try_open("198.51.100.1")
    limits.release_seat(KEY)
    clock.advance(61)
    assert limits.try_open(KEY)


def test_handshakes_30_per_60s_counts_refused_too() -> None:
    limits, clock = make(seats_per_key=1000)
    for _ in range(30):
        assert limits.try_open(KEY)
    assert not limits.try_open(KEY)
    clock.advance(59.9)
    assert not limits.try_open(KEY)  # refused attempts keep the window full
    clock.advance(60.0)
    assert limits.try_open(KEY)


def test_global_connections() -> None:
    limits, _ = make(max_connections=3)
    assert all(limits.try_open(f"k{i}") for i in range(3))
    assert limits.connections == 3
    assert not limits.try_open("k9")
    limits.connection_closed()
    assert limits.connections == 2
    assert limits.try_open("k9")


def test_creates_10_per_60s_refused_not_counted() -> None:
    limits, clock = make()
    for _ in range(10):
        assert limits.try_create(KEY)
        clock.advance(1)
    assert not limits.try_create(KEY)
    clock.advance(50.0)  # first one (t=0) expired at t=60; refused did not extend
    assert limits.try_create(KEY)
    assert not limits.try_create(KEY)


def test_joins_30_per_60s() -> None:
    limits, clock = make()
    for _ in range(30):
        assert limits.try_join(KEY)
    assert not limits.try_join(KEY)
    assert limits.try_join("198.51.100.1")
    clock.advance(60.0)
    assert limits.try_join(KEY)


def test_token_bucket_burst_then_refill() -> None:
    bucket = TokenBucket(200, 20.0, 0.0)
    assert all(bucket.take(1, 0.0) for _ in range(200))
    assert not bucket.take(1, 0.0)
    assert not bucket.take(1, 0.04)
    assert bucket.take(1, 0.05)


def test_key_bytes_bucket() -> None:
    limits, clock = make()
    assert limits.take_key_bytes(KEY, 524_288)
    assert not limits.take_key_bytes(KEY, 1)
    clock.advance(1.0)
    assert limits.take_key_bytes(KEY, 65_536)
    assert not limits.take_key_bytes(KEY, 1)
    assert limits.take_key_bytes("198.51.100.1", 1)


def test_reserve_and_free_global_budget() -> None:
    limits, _ = make(max_queued_bytes=100)
    assert limits.reserve(60)
    assert not limits.reserve(41)
    assert limits.queued_bytes == 60
    assert limits.reserve(40)
    limits.free(100)
    assert limits.queued_bytes == 0


def test_config_exposed() -> None:
    limits, _ = make()
    assert limits.config.max_rooms == 500


def test_sweep_drops_empty_keys_and_state_repr_has_no_ip() -> None:
    limits, clock = make()
    assert limits.try_open(KEY)
    assert limits.try_join(KEY)
    assert limits.try_create(KEY)
    assert limits.take_key_bytes(KEY, 10)
    assert KEY not in repr(limits)

    limits.sweep()
    assert limits.seats(KEY) == 1  # seat still held, state kept
    limits.release_seat(KEY)
    assert limits.seats(KEY) == 0
    clock.advance(120)
    limits.sweep()
    assert not any(KEY in repr(v) for v in vars(limits).values())
    assert limits.try_open(KEY)
