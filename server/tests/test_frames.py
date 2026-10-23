"""Raw incoming budget of a WebSocket connection, counted before frames are parsed (H1)."""

import os
import struct

import pytest

from app.frames import (
    RAW_BYTES_BURST,
    RAW_BYTES_RATE,
    RAW_FRAMES_BURST,
    RAW_FRAMES_RATE,
    FrameCounter,
    RawBudget,
)
from app.limits import LimitConfig

REQUEST = (
    b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
)


def frame(opcode: int, payload: bytes, *, fin: bool = True, masked: bool = True) -> bytes:
    """A client frame as it goes on the wire (masked by default)."""
    head = bytes([(0x80 if fin else 0) | opcode])
    mask_bit = 0x80 if masked else 0
    if len(payload) < 126:
        head += bytes([mask_bit | len(payload)])
    elif len(payload) < 1 << 16:
        head += bytes([mask_bit | 126]) + struct.pack("!H", len(payload))
    else:
        head += bytes([mask_bit | 127]) + struct.pack("!Q", len(payload))
    if not masked:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def stream() -> bytes:
    return (
        REQUEST
        + frame(0x1, b'{"type":"ping"}')
        + frame(0x9, b"")
        + frame(0x1, b"x" * 200)  # 16-bit length
        + frame(0x2, b"y" * 70_000)  # 64-bit length
        + frame(0x1, b"a", fin=False)
        + frame(0x0, b"b", fin=False)
        + frame(0x0, b"c")
        + frame(0xA, b"pong", masked=False)
    )


FRAMES_IN_STREAM = 8


def test_counts_frames_after_the_request() -> None:
    assert FrameCounter().feed(stream(), limit=100) == FRAMES_IN_STREAM


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 4096])
def test_counts_the_same_whatever_the_chunks(size: int) -> None:
    data = stream()
    counter = FrameCounter()
    total = sum(counter.feed(data[i : i + size], limit=100) for i in range(0, len(data), size))
    assert total == FRAMES_IN_STREAM


def test_request_end_split_across_chunks() -> None:
    counter = FrameCounter()
    assert counter.feed(REQUEST[:-3], limit=10) == 0
    assert counter.feed(REQUEST[-3:-1], limit=10) == 0
    assert counter.feed(REQUEST[-1:] + frame(0x9, b""), limit=10) == 1


def test_stops_counting_past_the_limit() -> None:
    counter = FrameCounter()
    pings = frame(0x9, b"") * 10_000
    assert counter.feed(REQUEST + pings, limit=5) == 6


def make_budget() -> RawBudget:
    return RawBudget(now=0.0)


def test_budget_numbers_sit_above_the_application_budgets() -> None:
    app = LimitConfig()
    assert 4 * app.msg_burst <= RAW_FRAMES_BURST
    assert 3 * app.msg_rate <= RAW_FRAMES_RATE
    # The app burst plus one read chunk (256 KiB) and one partial message still fit.
    assert app.bytes_burst + 262_144 + 65_536 + 14 * RAW_FRAMES_BURST <= RAW_BYTES_BURST
    assert 2 * app.bytes_rate <= RAW_BYTES_RATE
    assert (RAW_FRAMES_BURST, RAW_FRAMES_RATE) == (1024, 64.0)
    assert (RAW_BYTES_BURST, RAW_BYTES_RATE) == (1_048_576, 65_536.0)


def test_budget_admits_traffic_at_the_application_limits() -> None:
    budget = make_budget()
    message = frame(0x1, b"m" * 1_300)
    assert budget.admit(REQUEST, 0.0)
    assert budget.admit(message * 200, 0.0)  # the app's whole burst at once
    for i in range(1, 601):  # then 20 messages a second for 30 s, a pong each second
        now = i * 0.05
        assert budget.admit(message, now)
        if i % 20 == 0:
            assert budget.admit(frame(0xA, b"abcd"), now)


def test_budget_refuses_a_frame_flood() -> None:
    budget = make_budget()
    assert budget.admit(REQUEST, 0.0)
    assert budget.admit(frame(0x9, b"") * RAW_FRAMES_BURST, 0.0)
    assert not budget.admit(frame(0x9, b""), 0.0)


def test_budget_refuses_a_one_byte_fragment_flood() -> None:
    budget = make_budget()
    assert budget.admit(REQUEST + frame(0x1, b"a", fin=False), 0.0)
    fragments = frame(0x0, b"b", fin=False) * 2_000
    assert not budget.admit(fragments, 0.1)


def test_budget_refuses_bytes_over_the_burst() -> None:
    budget = make_budget()
    assert budget.admit(REQUEST, 0.0)
    big = frame(0x2, b"z" * 60_000)
    chunks = [budget.admit(big, 0.0) for _ in range(20)]
    assert chunks[:16] == [True] * 16
    assert chunks[-1] is False


def test_budget_refills_over_time() -> None:
    budget = make_budget()
    assert budget.admit(REQUEST, 0.0)
    assert budget.admit(frame(0x9, b"") * (RAW_FRAMES_BURST - 10), 0.0)
    assert budget.admit(frame(0x9, b"") * 10, 0.0)
    assert budget.admit(frame(0x9, b"") * 64, 1.0)
