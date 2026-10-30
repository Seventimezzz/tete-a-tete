"""Raw incoming budget of a WebSocket connection, counted before frames are parsed.

Pure logic: time comes from the caller. The budget is a backstop under the per-message limits
of the application (app/limits.py), which never see control frames (uvicorn answers PING
itself) or the frames of a fragmented message; it also caps the frames of one message. It
holds only counters and a frame header split across reads, never payload.
"""

from typing import Final

from app.limits import TokenBucket

# Why these numbers. The application lets a connection send 200 messages at once and 20 a
# second after, 256 KiB at once and 32 KiB a second (LimitConfig). Legitimate traffic at those
# limits must be cut by the application's 1008, never here, so the raw budget sits above them:
#
# - Frames: 1024 at once, 64 a second. One frame per message is what clients send; this leaves
#   room for fragmenting up to five frames a message in the burst and three at the rate, plus
#   control frames: one PONG per server ping (every 15 s), a client's own keepalive PING, CLOSE.
#   A PING or 1-byte-fragment flood is cut after 1024 frames instead of costing a CPU core.
# - Bytes: 1 MiB at once, 64 KiB a second. The raw count runs ahead of the application by what
#   uvicorn has read but the application has not handled yet: at most one read of the event
#   loop (256 KiB) and one partial message (64 KiB). 256 KiB + 256 KiB + 64 KiB + 14 bytes of
#   framing per frame (14 KiB for 1024 frames) is about 590 KiB, under 1 MiB. Twice the
#   application rate covers framing and control frames. With permessage-deflate the wire
#   carries fewer bytes than the application counts, so the raw count is never larger.
RAW_FRAMES_BURST: Final = 1024
RAW_FRAMES_RATE: Final = 64.0
RAW_BYTES_BURST: Final = 1_048_576
RAW_BYTES_RATE: Final = 65_536.0

# Frames of one message: the first and its continuations. Without a cap a client keeps one
# message open for minutes with continuations under the raw rate, and uvicorn holds every
# fragment as a separate object (megabytes from a few hundred KiB on the wire). Legitimate
# messages are at most 64 KiB (--ws-max-size). Chromium, whose WebSocket the client uses,
# sends them as one frame: Edge 155 put each of 2,436 messages of 15 B to 64 KiB in a single
# frame, with and without permessage-deflate and with a reader slow enough to fill its send
# buffer; it split only messages over about 131,000 bytes, and those into frames of up to
# 131,000 bytes plus a short one where its buffer wraps. The websockets client sends a str as
# one frame (an iterable as a frame per item and an empty last one). 64 leaves a 64 KiB
# message room for 1 KiB frames.
MESSAGE_FRAMES_MAX: Final = 64

_REQUEST_END: Final = b"\r\n\r\n"
_LEN16: Final = 126
_LEN64: Final = 127
_FIN: Final = 0x80
_OPCODE: Final = 0x0F
_CONTROL: Final = 0x08  # opcodes 0x8-0xF: CLOSE, PING, PONG and reserved
_CONTINUATION: Final = 0x00


def _layout(header: bytes | bytearray) -> tuple[int, int] | None:
    """(header length, payload length) of a frame once `header` holds its length fields."""
    if len(header) < 2:
        return None
    second = header[1]
    short = second & 0x7F
    extended = 2 if short == _LEN16 else 8 if short == _LEN64 else 0
    if len(header) < 2 + extended:
        return None
    size = 2 + extended + (4 if second & 0x80 else 0)
    length = int.from_bytes(header[2 : 2 + extended]) if extended else short
    return size, length


class FrameCounter:
    """Counts the frames that start in a byte stream: first the HTTP request of the handshake
    up to its empty line, then frame headers; payloads are skipped, not looked at. Also counts
    the data frames of the message in progress against MESSAGE_FRAMES_MAX."""

    def __init__(self) -> None:
        self._in_request = True
        self._tail = b""  # the last bytes of the request so far, to find its end across reads
        self._header = bytearray()  # a frame header split across reads
        self._skip = 0  # payload bytes of the current frame still to come
        self._message_frames = 0  # data frames of the unfinished message, 0 between messages
        self.too_fragmented = False  # a message ran past MESSAGE_FRAMES_MAX frames

    def __repr__(self) -> str:
        return "FrameCounter()"

    def feed(self, data: bytes, limit: int) -> int:
        """Frames starting in `data`. Stops at limit + 1, or at the frame that takes a message
        past MESSAGE_FRAMES_MAX, setting `too_fragmented`: the caller refuses either way."""
        at = 0
        if self._in_request:
            joined = self._tail + data
            end = joined.find(_REQUEST_END)
            if end < 0:
                self._tail = joined[-(len(_REQUEST_END) - 1) :]
                return 0
            at = end + len(_REQUEST_END) - len(self._tail)
            self._in_request = False
            self._tail = b""
        count = 0
        size = len(data)
        while at < size:
            if self._skip:
                step = min(self._skip, size - at)
                self._skip -= step
                at += step
                continue
            if not self._header:
                count += 1
                if count > limit:
                    return count
                if not self._within_message(data[at]):
                    self.too_fragmented = True
                    return count
            layout = None
            while at < size:
                self._header.append(data[at])
                at += 1
                layout = _layout(self._header)
                if layout is not None and len(self._header) == layout[0]:
                    break
            if layout is None or len(self._header) < layout[0]:
                break  # the header continues in the next read
            self._header.clear()
            self._skip = layout[1]
        return count

    def _within_message(self, first: int) -> bool:
        """Count a frame by its first byte (FIN and opcode); False past MESSAGE_FRAMES_MAX.
        Control frames may come between the fragments of a message: they neither count nor
        end it. A data frame with FIN ends its message."""
        opcode = first & _OPCODE
        if opcode & _CONTROL:
            return True
        self._message_frames = self._message_frames + 1 if opcode == _CONTINUATION else 1
        if self._message_frames > MESSAGE_FRAMES_MAX:
            return False
        if first & _FIN:
            self._message_frames = 0
        return True


class RawBudget:
    """Bytes and frames a connection may send, refilled over time. Over either, or a message
    in more than MESSAGE_FRAMES_MAX frames: refuse."""

    def __init__(self, now: float) -> None:
        self._bytes = TokenBucket(RAW_BYTES_BURST, RAW_BYTES_RATE, now)
        self._frames = TokenBucket(RAW_FRAMES_BURST, RAW_FRAMES_RATE, now)
        self._counter = FrameCounter()

    def __repr__(self) -> str:
        return "RawBudget()"

    def admit(self, data: bytes, now: float) -> bool:
        """Take what `data` costs. After a refusal the connection must be dropped: the frame
        count stopped in the middle of `data`."""
        if not self._bytes.take(len(data), now):
            return False
        frames = self._counter.feed(data, int(self._frames.available(now)))
        return not self._counter.too_fragmented and self._frames.take(frames, now)
