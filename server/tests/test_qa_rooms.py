"""QA, stage 1: boundary, negative and privacy cases of rooms, tokens, limits and models.

Written by qa-engineer on top of the implementers' tests; failing cases are pinned with
xfail(strict=True) and a QA-<n> reference to the report.
"""

import json
from typing import Any

import pytest
from pydantic import JsonValue

from app.limits import LimitConfig, Limits
from app.protocol import (
    ClientCreate,
    ClientJoin,
    ClientLeave,
    ClientSignal,
    NetInfo,
    PeerInfo,
    ServerJoined,
    ServerPeerJoined,
    parse_client_message,
)
from app.rooms import WAIT_QUEUE_BYTES, Hub, HubStats
from app.tokens import _sign, issue_token, verify_token
from tests.fakes import START, FakeClock, FakeConn

SECRET = b"q" * 32
NET = NetInfo(nat=None, upnp=None, cgnat=None, double_nat=None, vpn=None)
ROOM = "qrstuvwxyz23"
ROOM2 = "rstuvwxyz234"
NAME = "ИмяДляУтечки"
MAX_FRAME = 65_536


class Env:
    def __init__(self, config: LimitConfig | None = None) -> None:
        self.clock = FakeClock()
        self.limits = Limits(config or LimitConfig(), self.clock)
        self.hub = Hub(secret=SECRET, clock=self.clock, limits=self.limits)

    def open(self, key: str = "k") -> FakeConn:
        assert self.limits.try_open(key)
        return FakeConn(key)

    def drop(self, conn: FakeConn) -> None:
        self.limits.connection_closed()
        self.hub.disconnected(conn)

    def at(self, offset: float) -> None:
        self.clock.advance(START + offset - self.clock.now())

    def join(
        self, conn: FakeConn, room_id: str = ROOM, name: str = "Аня", token: str | None = None
    ) -> None:
        self.hub.handle(
            conn, ClientJoin(type="join", room_id=room_id, name=name, net=NET, resume_token=token)
        )

    def create(self, conn: FakeConn) -> None:
        self.hub.handle(conn, ClientCreate(type="create", name="Аня", net=NET))


class RawConn(FakeConn):
    """Also keeps the exact text the hub hands to the connection."""

    def __init__(self, key: str = "k") -> None:
        super().__init__(key)
        self.texts: list[str] = []

    def send(self, text: str) -> None:
        self.texts.append(text)
        super().send(text)


def joined(conn: FakeConn) -> dict[str, Any]:
    return conn.of_type("joined")[-1]


def errors(conn: FakeConn) -> list[str]:
    return [m["code"] for m in conn.of_type("error")]


# --- privacy of models (spec "Логи": `name` in Pydantic models is repr=False)


@pytest.mark.xfail(
    strict=True,
    reason="QA-1: name участника виден в repr/str моделей протокола (ClientCreate, ClientJoin, "
    "PeerInfo, ServerPeerJoined, ServerJoined); спецификация требует repr=False для name",
)
def test_model_repr_and_str_hide_the_name() -> None:
    peer = PeerInfo(id="abcdefgh", name=NAME, muted=False)
    models = [
        ClientCreate(type="create", name=NAME, net=NET),
        ClientJoin(type="join", room_id=ROOM, name=NAME, net=NET),
        peer,
        ServerPeerJoined(id="abcdefgh", name=NAME),
        ServerJoined(
            room_id=ROOM,
            self_id="bcdefghj",
            resumed=False,
            resume_token="x.y",  # noqa: S106 - not a secret
            peers=[peer],
        ),
    ]
    leaking = [type(m).__name__ for m in models if NAME in repr(m) + str(m)]
    assert leaking == []


# --- signal forwarding: size of the re-serialized message


def _signal_frame(numbers: int) -> str:
    """A client signal of short numbers that the server dumps longer (9e15 -> 9000...0.0)."""
    items = ",".join(["9e15"] * numbers)
    return f'{{"type":"signal","to":"PEER","data":{{"candidate":[{items}]}}}}'


@pytest.mark.xfail(
    strict=True,
    reason="QA-3: пересобранный signal в ~3,8 раза больше принятого кадра: кадр < 64 КБ "
    "уходит адресату сообщением ~250 КБ, мимо лимита размера и байтовых бакетов",
)
def test_forwarded_signal_is_not_larger_than_the_message_limit() -> None:
    env = Env()
    a, b = env.open(), RawConn()
    assert env.limits.try_open(b.key)
    env.join(a)
    env.join(b)
    frame = _signal_frame(13_000).replace("PEER", joined(b)["selfId"])
    assert len(frame.encode()) < MAX_FRAME  # uvicorn lets it in
    env.hub.handle(a, parse_client_message(frame))
    assert b.of_type("signal") != []
    assert len(b.texts[-1].encode()) <= MAX_FRAME


def test_forwarded_signal_keeps_data_values() -> None:
    """Whatever the textual form, the numbers and strings reach the addressee unchanged."""
    env = Env()
    a, b = env.open(), env.open()
    env.join(a)
    env.join(b)
    raw = (
        '{"type":"signal","to":"PEER","data":{"n":[1e5,-0,0.1,12345678901234567890123],'
        '"s":"\\u0041\\/\\n\\u2028😀","e":{},"l":[]}}'
    ).replace("PEER", joined(b)["selfId"])
    env.hub.handle(a, parse_client_message(raw))
    assert b.of_type("signal")[0]["data"] == json.loads(raw)["data"]


# --- join table and tokens: cases the implementers' tests do not pin


def test_token_with_swapped_peer_id_is_invalid() -> None:
    good = issue_token(SECRET, ROOM, "abcdefgh")
    swapped = "bcdefghj" + good[len("abcdefgh") :]
    assert verify_token(SECRET, ROOM, swapped) is None


def test_token_shape_checked_before_signature() -> None:
    """A peer id outside the alphabet is refused even with a valid signature over it."""
    for peer_id in ("abcdefg1", "ABCDEFGH", "abcdefgl", "abcdefg0"):
        assert verify_token(SECRET, ROOM, f"{peer_id}.{_sign(SECRET, ROOM, peer_id)}") is None
    for peer_id in ("abcdefg", "abcdefghj"):  # 7 and 9 characters
        assert verify_token(SECRET, ROOM, f"{peer_id}.{_sign(SECRET, ROOM, peer_id)}") is None


def test_join_with_token_of_open_place_in_full_room_resumes() -> None:
    """Taking over one's own place needs no free seat."""
    env = Env()
    conns = [env.open() for _ in range(5)]
    for conn in conns:
        env.join(conn)
    again = env.open()
    env.join(again, token=joined(conns[2])["resumeToken"])
    assert errors(again) == []
    assert joined(again)["resumed"] is True
    assert conns[2].closed == 4000
    assert env.hub.stats() == HubStats(rooms=1, peers=5, waiting=0)


def test_waiting_place_takes_a_seat_and_a_sixth_gets_room_full() -> None:
    env = Env()
    conns = [env.open() for _ in range(5)]
    for conn in conns:
        env.join(conn)
    env.drop(conns[4])
    env.at(14.9)
    sixth = env.open()
    env.join(sixth)
    assert errors(sixth) == ["room-full"]
    back = env.open()
    env.join(back, token=joined(conns[4])["resumeToken"])
    assert joined(back)["resumed"] is True


def test_room_full_and_rate_limited_answers_leave_no_state() -> None:
    env = Env(LimitConfig(creates_per_window=0))
    refused = env.open()
    env.join(refused, room_id=ROOM2)  # a new room, no create budget
    env.create(refused)
    assert errors(refused) == ["rate-limited", "rate-limited"]
    assert env.hub.stats() == HubStats(0, 0, 0)
    # the refused connection can still enter an existing room
    env2 = Env()
    conns = [env2.open() for _ in range(5)]
    for conn in conns:
        env2.join(conn)
    late = env2.open()
    env2.join(late)
    assert errors(late) == ["room-full"]
    env2.hub.handle(conns[0], ClientLeave(type="leave"))
    env2.join(late)
    assert joined(late)["resumed"] is False
    env2.drop(late)  # its drop starts a waiting place, not a leak
    assert env2.hub.stats() == HubStats(rooms=1, peers=5, waiting=1)


def test_global_room_limit_does_not_spend_the_create_budget() -> None:
    env = Env(LimitConfig(max_rooms=1, creates_per_window=1))
    holder = env.open("other")
    env.join(holder, room_id=ROOM2)  # the only room the server may have
    c = env.open()
    env.create(c)
    assert errors(c) == ["rate-limited"]
    env.hub.handle(holder, ClientLeave(type="leave"))
    env.create(c)  # the refused attempt above did not use the one create of the window
    assert errors(c) == ["rate-limited"]  # unchanged: no new error
    assert joined(c)["resumed"] is False


def test_create_window_boundary_59_9_and_60_seconds() -> None:
    env = Env(LimitConfig(creates_per_window=2))
    c = env.open()
    for _ in range(2):
        env.create(c)
        env.hub.handle(c, ClientLeave(type="leave"))
    env.at(59.9)
    env.create(c)
    assert errors(c) == ["rate-limited"]
    env.at(60.0)
    env.create(c)
    assert errors(c) == ["rate-limited"]
    assert len(c.of_type("joined")) == 3


def test_waiting_queue_byte_boundary_is_inclusive() -> None:
    env = Env()
    w, a = env.open(), env.open()
    env.join(w)
    env.join(a)
    w_id = joined(w)["selfId"]
    env.drop(w)
    probe: dict[str, JsonValue] = {"s": ""}
    env.hub.handle(a, ClientSignal(type="signal", to=w_id, data=probe))
    one = env.limits.queued_bytes  # size of a signal with an empty string
    pad = WAIT_QUEUE_BYTES - one * 2  # the next one fills the queue to the byte
    env.hub.handle(a, ClientSignal(type="signal", to=w_id, data={"s": "x" * pad}))
    assert env.limits.queued_bytes == WAIT_QUEUE_BYTES
    env.hub.handle(a, ClientSignal(type="signal", to=w_id, data={"s": ""}))
    assert env.limits.queued_bytes == WAIT_QUEUE_BYTES  # one byte over: dropped
    back = env.open()
    env.join(back, token=joined(w)["resumeToken"])
    assert len(back.of_type("signal")) == 2
    assert env.limits.queued_bytes == 0


def test_queue_of_expired_place_frees_global_bytes() -> None:
    env = Env()
    w, a = env.open(), env.open()
    env.join(w)
    env.join(a)
    env.drop(w)
    env.hub.handle(a, ClientSignal(type="signal", to=joined(w)["selfId"], data={"s": "y" * 999}))
    assert env.limits.queued_bytes > 0
    env.at(15.0)
    env.hub.sweep()
    assert env.limits.queued_bytes == 0
    assert env.limits.seats("k") == 1  # only a's connection


def test_leave_during_wait_of_another_keeps_room_until_sweep() -> None:
    env = Env()
    a, b = env.open(), env.open()
    env.join(a)
    env.join(b)
    env.drop(b)
    env.hub.handle(a, ClientLeave(type="leave"))
    assert env.hub.stats() == HubStats(rooms=1, peers=1, waiting=1)
    back = env.open()
    env.join(back, token=joined(b)["resumeToken"])
    assert (joined(back)["resumed"], joined(back)["peers"]) == (True, [])
    env.drop(back)
    env.at(30.0)
    env.hub.sweep()
    assert env.hub.stats() == HubStats(0, 0, 0)
    assert env.limits.seats("k") == 1  # a's connection is still open
