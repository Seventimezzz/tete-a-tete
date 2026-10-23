"""Disconnects, the waiting period, resume by token and the sweep (task 5)."""

import random
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import JsonValue

from app import rooms as rooms_module
from app.limits import LimitConfig, Limits
from app.protocol import (
    CLOSE_REPLACED,
    ClientCreate,
    ClientJoin,
    ClientLeave,
    ClientMute,
    ClientSignal,
    NetInfo,
    new_peer_id,
)
from app.rooms import LEFT_MAX, Hub, HubStats
from app.tokens import issue_token
from tests.fakes import START, FakeClock, FakeConn

SECRET = b"t" * 32
NET = NetInfo(nat=None, upnp=None, cgnat=None, double_nat=None, vpn=None)
ROOM = "abcdefghjkmn"
ROOM2 = "bcdefghjkmnp"
ROOM3 = "cdefghjkmnpq"


@dataclass
class Env:
    hub: Hub
    clock: FakeClock
    limits: Limits

    def open(self, key: str = "k") -> FakeConn:
        """A connection as the /ws layer opens it: it takes a slot of its key."""
        assert self.limits.try_open(key)
        return FakeConn(key)

    def drop(self, conn: FakeConn) -> None:
        """The transport closed, as the /ws layer reports it."""
        self.limits.connection_closed()
        self.hub.disconnected(conn)

    def at(self, offset: float) -> None:
        """Set the clock to START + offset exactly."""
        self.clock.advance(START + offset - self.clock.now())


def make_env(config: LimitConfig | None = None, clock: FakeClock | None = None) -> Env:
    clock = clock or FakeClock()
    limits = Limits(config or LimitConfig(), clock)
    return Env(Hub(secret=SECRET, clock=clock, limits=limits), clock, limits)


def join(
    env: Env,
    conn: FakeConn,
    room_id: str = ROOM,
    name: str = "Аня",
    token: str | None = None,
    net: NetInfo = NET,
) -> None:
    env.hub.handle(
        conn,
        ClientJoin(type="join", room_id=room_id, name=name, net=net, resume_token=token),
    )


def joined(conn: FakeConn) -> dict[str, Any]:
    return conn.of_type("joined")[-1]


def self_id(conn: FakeConn) -> str:
    peer_id: str = joined(conn)["selfId"]
    return peer_id


def token(conn: FakeConn) -> str:
    resume_token: str = joined(conn)["resumeToken"]
    return resume_token


def signal(env: Env, conn: FakeConn, to: str, data: dict[str, JsonValue]) -> None:
    env.hub.handle(conn, ClientSignal(type="signal", to=to, data=data))


def signals_data(conn: FakeConn) -> list[Any]:
    return [m["data"] for m in conn.of_type("signal")]


def test_disconnect_keeps_place_silently() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a, name="Аня")
    join(env, b, name="Боря")
    before = len(a.sent)
    env.drop(b)
    assert len(a.sent) == before
    assert env.hub.stats() == HubStats(rooms=1, peers=2, waiting=1)
    c = env.open()
    join(env, c, name="Вика")
    assert {"id": self_id(b), "name": "Боря", "muted": False} in joined(c)["peers"]


def test_signal_to_waiting_peer_is_queued_and_delivered_in_order() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    a_id, b_id = self_id(a), self_id(b)
    env.drop(b)
    for n in range(3):
        signal(env, a, b_id, {"n": n})
    assert env.limits.queued_bytes > 0
    a_before = len(a.sent)
    b2 = env.open()
    join(env, b2, token=token(b))
    assert [m["type"] for m in b2.sent] == ["joined", "signal", "signal", "signal"]
    j = joined(b2)
    assert j["resumed"] is True
    assert j["selfId"] == b_id
    assert j["peers"] == [{"id": a_id, "name": "Аня", "muted": False}]
    assert [m["from"] for m in b2.of_type("signal")] == [a_id] * 3
    assert signals_data(b2) == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert env.limits.queued_bytes == 0
    assert len(a.sent) == a_before  # the others hear nothing about a resume
    signal(env, a, b_id, {"n": 3})
    assert signals_data(b2)[-1] == {"n": 3}
    assert len(b2.of_type("signal")) == 4  # the queue was delivered exactly once


def test_waiting_queue_caps() -> None:
    env = make_env()
    w, a, b, c = (env.open() for _ in range(4))
    for conn in (w, a, b, c):
        join(env, conn)
    w_id = self_id(w)
    env.drop(w)
    for n in range(30):
        signal(env, a, w_id, {"a": n})  # only 25 from one sender
    for n in range(25):
        signal(env, b, w_id, {"b": n})  # another sender still fits: 50 in all
    signal(env, c, w_id, {"c": 0})  # the 51st does not
    w2 = env.open()
    join(env, w2, token=token(w))
    assert signals_data(w2) == [{"a": n} for n in range(25)] + [{"b": n} for n in range(25)]
    assert env.limits.queued_bytes == 0


def test_waiting_queue_byte_cap() -> None:
    env = make_env()
    w, a, b = (env.open() for _ in range(3))
    for conn in (w, a, b):
        join(env, conn)
    w_id = self_id(w)
    env.drop(w)
    big = "x" * 60_000
    for n in range(5):
        signal(env, a, w_id, {"n": n, "s": big})  # 4 fit into 256 KB, the 5th does not
    assert 240_000 < env.limits.queued_bytes <= 262_144
    signal(env, b, w_id, {"small": True})  # a smaller one still fits
    w2 = env.open()
    join(env, w2, token=token(w))
    got = signals_data(w2)
    assert [d.get("n") for d in got] == [0, 1, 2, 3, None]
    assert got[-1] == {"small": True}
    assert env.limits.queued_bytes == 0


def test_waiting_queue_respects_global_byte_limit() -> None:
    env = make_env(LimitConfig(max_queued_bytes=100))
    w, a = env.open(), env.open()
    join(env, w)
    join(env, a)
    env.drop(w)
    signal(env, a, self_id(w), {"s": "x" * 200})
    assert env.limits.queued_bytes == 0
    w2 = env.open()
    join(env, w2, token=token(w))
    assert w2.of_type("signal") == []


def test_sweep_keeps_at_14_9s_removes_at_15s() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    b_id = self_id(b)
    env.drop(b)
    env.at(14.9)
    env.hub.sweep()
    assert a.of_type("peer-left") == []
    assert env.hub.stats().waiting == 1
    env.at(15.0)
    env.hub.sweep()
    env.hub.sweep()
    assert a.of_type("peer-left") == [{"type": "peer-left", "id": b_id}]
    assert env.hub.stats() == HubStats(rooms=1, peers=1, waiting=0)
    b2 = env.open()
    join(env, b2, token=token(b))  # the id went to `left`
    assert joined(b2)["resumed"] is False
    assert self_id(b2) != b_id


def test_sweep_deletes_room_left_empty() -> None:
    env = make_env()
    a = env.open()
    join(env, a)
    env.drop(a)
    env.at(15.0)
    env.hub.sweep()
    assert env.hub.stats() == HubStats(rooms=0, peers=0, waiting=0)


def test_resume_at_14_99s_keeps_place() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    env.drop(b)
    env.at(14.99)
    b2 = env.open()
    join(env, b2, token=token(b))
    assert joined(b2)["resumed"] is True
    assert self_id(b2) == self_id(b)
    assert a.of_type("peer-left") == []
    env.at(30.0)
    env.hub.sweep()  # the place is on line again, the sweep leaves it alone
    assert a.of_type("peer-left") == []
    assert env.hub.stats() == HubStats(rooms=1, peers=2, waiting=0)


def test_resume_at_exactly_15s_before_sweep_is_new_peer() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    b_id = self_id(b)
    env.drop(b)
    env.at(15.0)
    b2 = env.open()
    join(env, b2, token=token(b))
    j = joined(b2)
    assert j["resumed"] is False
    assert j["selfId"] != b_id
    assert j["peers"] == [{"id": self_id(a), "name": "Аня", "muted": False}]
    assert a.of_type("peer-left") == [{"type": "peer-left", "id": b_id}]
    assert [m["type"] for m in a.sent[-2:]] == ["peer-left", "peer-joined"]
    env.hub.sweep()
    assert len(a.of_type("peer-left")) == 1
    assert env.hub.stats() == HubStats(rooms=1, peers=2, waiting=0)


def test_stale_place_counts_as_free_for_a_newcomer() -> None:
    env = make_env()
    conns = [env.open() for _ in range(5)]
    for conn in conns:
        join(env, conn)
    env.drop(conns[0])
    env.at(15.0)
    newcomer = env.open()
    join(env, newcomer)  # no sweep yet, but the stale place is free
    assert newcomer.of_type("error") == []
    assert joined(newcomer)["resumed"] is False
    for conn in conns[1:]:
        assert len(conn.of_type("peer-left")) == 1


def test_resume_at_15s_into_otherwise_empty_room_matches_sweep() -> None:
    """With the sweep first, the room is gone and the token recreates it (decision 10)."""
    results = []
    for sweep_first in (False, True):
        env = make_env()
        a = env.open()
        join(env, a)
        env.drop(a)
        env.at(15.0)
        if sweep_first:
            env.hub.sweep()
        a2 = env.open()
        join(env, a2, token=token(a))
        j = joined(a2)
        results.append((j["resumed"], j["selfId"] == self_id(a), j["peers"]))
    assert results[0] == results[1] == (True, True, [])


def test_resume_after_leave_is_new_peer() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    b_id = self_id(b)
    env.hub.handle(b, ClientLeave(type="leave"))
    join(env, b, token=token(b))
    assert joined(b)["resumed"] is False
    assert self_id(b) != b_id
    assert a.of_type("peer-left") == [{"type": "peer-left", "id": b_id}]
    assert a.of_type("peer-joined")[-1]["id"] == self_id(b)


def test_resume_while_old_conn_open_closes_it_with_4000() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    a_before = len(a.sent)
    b2 = env.open()
    join(env, b2, token=token(b))
    assert b.closed == CLOSE_REPLACED
    assert b2.closed is None
    j = joined(b2)
    assert j["resumed"] is True
    assert j["selfId"] == self_id(b)
    assert len(a.sent) == a_before  # no peer-left, no peer-joined
    signal(env, a, self_id(b), {"to": "new"})
    assert signals_data(b2) == [{"to": "new"}]
    assert b.of_type("signal") == []
    assert env.hub.stats() == HubStats(rooms=1, peers=2, waiting=0)


def test_replaced_conn_disconnect_does_not_start_waiting() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    b2 = env.open()
    join(env, b2, token=token(b))
    env.drop(b)
    assert env.hub.stats() == HubStats(rooms=1, peers=2, waiting=0)
    signal(env, a, self_id(b2), {"n": 1})
    assert signals_data(b2) == [{"n": 1}]
    env.at(60.0)
    env.hub.sweep()
    assert a.of_type("peer-left") == []


def test_two_joins_with_same_token_last_wins() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    env.drop(b)
    a_before = len(a.sent)
    n1, n2 = env.open(), env.open()
    join(env, n1, token=token(b))
    join(env, n2, token=token(b))
    assert joined(n1)["resumed"] is True
    assert joined(n2)["resumed"] is True
    assert self_id(n1) == self_id(n2) == self_id(b)
    assert n1.closed == CLOSE_REPLACED
    assert n2.closed is None
    assert env.hub.stats() == HubStats(rooms=1, peers=2, waiting=0)
    assert len(a.sent) == a_before
    env.drop(n1)
    assert env.hub.stats().waiting == 0
    signal(env, a, self_id(b), {"n": 1})
    assert signals_data(n2) == [{"n": 1}]
    assert n1.of_type("signal") == []


def test_signal_during_switch_arrives_after_queue() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    b_id = self_id(b)
    env.drop(b)
    signal(env, a, b_id, {"n": 1})
    b2 = env.open()
    join(env, b2, token=token(b))
    signal(env, a, b_id, {"n": 2})
    assert [m["type"] for m in b2.sent] == ["joined", "signal", "signal"]
    assert signals_data(b2) == [{"n": 1}, {"n": 2}]
    # with the old connection still open: what it got stays there, the rest goes to the new one
    signal(env, a, b_id, {"n": 3})
    b3 = env.open()
    join(env, b3, token=token(b))
    signal(env, a, b_id, {"n": 4})
    assert signals_data(b2) == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert [m["type"] for m in b3.sent] == ["joined", "signal"]
    assert signals_data(b3) == [{"n": 4}]


def test_resume_into_recreated_room_keeps_id_and_notifies() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a, name="Аня")
    join(env, b, name="Боря")
    a_id, b_id = self_id(a), self_id(b)
    restarted = make_env()  # a new process with the same secret
    a2, b2 = restarted.open(), restarted.open()
    join(restarted, a2, name="Аня", token=token(a))
    ja = joined(a2)
    assert (ja["resumed"], ja["selfId"], ja["peers"], ja["roomId"]) == (True, a_id, [], ROOM)
    join(restarted, b2, name="Боря", token=token(b))
    jb = joined(b2)
    assert (jb["resumed"], jb["selfId"]) == (True, b_id)
    assert jb["peers"] == [{"id": a_id, "name": "Аня", "muted": False}]
    assert a2.of_type("peer-joined") == [{"type": "peer-joined", "id": b_id, "name": "Боря"}]
    assert ja["resumeToken"] == token(a)  # deterministic


def test_resume_into_full_recreated_room_is_room_full() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    restarted = make_env()
    a2 = restarted.open()
    join(restarted, a2, token=token(a))
    for _ in range(4):
        join(restarted, restarted.open())
    b2 = restarted.open()
    join(restarted, b2, token=token(b))
    assert b2.of_type("joined") == []
    assert b2.of_type("error") == [{"type": "error", "code": "room-full"}]


def test_token_for_deleted_room_recreates_it() -> None:
    env = make_env(LimitConfig(creates_per_window=1))
    a = env.open()
    peer_id = "abcdefgh"
    join(env, a, token=issue_token(SECRET, ROOM, peer_id))
    j = joined(a)
    assert (j["resumed"], j["selfId"], j["peers"]) == (True, peer_id, [])
    # the token join did not spend the creation budget
    c = env.open()
    env.hub.handle(c, ClientCreate(type="create", name="Вика", net=NET))
    assert c.of_type("error") == []
    assert joined(c)["resumed"] is False


def test_token_recreate_respects_global_room_limit() -> None:
    env = make_env(LimitConfig(max_rooms=1))
    join(env, env.open(), room_id=ROOM2)
    a = env.open()
    join(env, a, token=issue_token(SECRET, ROOM, "abcdefgh"))
    assert a.of_type("error") == [{"type": "error", "code": "rate-limited"}]
    assert env.hub.stats().rooms == 1


def test_invalid_token_joins_as_new_peer() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    b_id = self_id(b)
    for bad in (
        issue_token(SECRET, ROOM2, b_id),  # issued for another room
        issue_token(b"o" * 32, ROOM, b_id),  # another secret
        "garbage",
    ):
        c = env.open()
        join(env, c, token=bad)
        assert joined(c)["resumed"] is False
        assert self_id(c) != b_id
        assert b.closed is None
        env.hub.handle(c, ClientLeave(type="leave"))
    # an invalid token on a missing room goes through the creation limit
    limited = make_env(LimitConfig(creates_per_window=0))
    d = limited.open()
    join(limited, d, token=issue_token(b"o" * 32, ROOM, b_id))
    assert d.of_type("error") == [{"type": "error", "code": "rate-limited"}]


def test_resume_updates_net_keeps_name() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a, name="Аня")
    join(env, b, name="Боря")
    b_id = self_id(b)
    env.drop(b)
    new_net = NetInfo(nat="cone", upnp=True, cgnat=False, double_nat=False, vpn=False)
    b2 = env.open()
    join(env, b2, name="Другое", token=token(b), net=new_net)
    c = env.open()
    join(env, c, name="Вика")
    assert {"id": b_id, "name": "Боря", "muted": False} in joined(c)["peers"]
    assert env.hub._rooms[ROOM].peers[b_id].net == new_net


def test_resume_keeps_muted() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    env.hub.handle(b, ClientMute(type="mute", muted=True))
    env.drop(b)
    b2 = env.open()
    join(env, b2, token=token(b))
    a2 = env.open()
    join(env, a2, token=token(a))
    assert joined(a2)["resumed"] is True
    assert {"id": self_id(b), "name": "Аня", "muted": True} in joined(a2)["peers"]


def test_seats_follow_places() -> None:
    env = make_env()
    seats = env.limits.seats
    a, b = env.open("k"), env.open("k")
    join(env, a)
    join(env, b)
    assert seats("k") == 2
    # a drop in a room: the waiting place keeps the slot
    env.drop(a)
    assert seats("k") == 2
    # a return on a new connection: it brings its own slot, the place lets go of its one
    a2 = env.open("k")
    assert seats("k") == 3
    join(env, a2, token=token(a))
    assert seats("k") == 2
    # leave does not touch slots: the connection keeps its own
    env.hub.handle(b, ClientLeave(type="leave"))
    assert seats("k") == 2
    join(env, b)
    assert seats("k") == 2
    # replacing an open connection: the old one's slot goes when it closes, exactly once
    b2 = env.open("k")
    join(env, b2, token=token(b))
    assert seats("k") == 3
    env.drop(b)
    assert seats("k") == 2
    # the sweep removes a waiting place and its slot
    env.drop(a2)
    assert seats("k") == 2
    env.at(15.0)
    env.hub.sweep()
    assert seats("k") == 1
    # a stale place freed by a join lets go of its slot too
    env.drop(b2)
    assert seats("k") == 1
    env.at(30.0)
    c = env.open("other")
    join(env, c)
    assert seats("k") == 0
    # a drop outside a room releases the connection's slot
    env.hub.handle(c, ClientLeave(type="leave"))
    env.drop(c)
    assert seats("other") == 0
    assert env.hub.stats() == HubStats(0, 0, 0)


@dataclass
class Churn:
    env: Env
    rng: random.Random
    keys: list[str]
    open: list[FakeConn] = field(default_factory=list)
    tokens: list[tuple[str, str]] = field(default_factory=list)
    seen: list[FakeConn] = field(default_factory=list)

    def new_conn(self) -> FakeConn | None:
        key = self.rng.choice(self.keys)
        if not self.env.limits.try_open(key):
            return None
        conn = FakeConn(key)
        self.open.append(conn)
        self.seen.append(conn)
        return conn

    def remember(self, conn: FakeConn) -> None:
        for m in conn.of_type("joined"):
            pair = (m["roomId"], m["resumeToken"])
            if pair not in self.tokens:
                self.tokens.append(pair)

    def close_replaced(self) -> None:
        for conn in [c for c in self.open if c.closed is not None]:
            self.drop(conn)

    def drop(self, conn: FakeConn) -> None:
        self.open.remove(conn)
        self.env.drop(conn)


def test_random_churn_returns_counters_to_zero() -> None:
    clock = FakeClock()
    config = LimitConfig(
        seats_per_key=10**6,
        handshakes_per_window=10**6,
        joins_per_window=10**6,
        creates_per_window=10**6,
    )
    env = make_env(config, clock)
    rng = random.Random(1)  # noqa: S311 - a reproducible test sequence
    keys = ["k0", "k1", "k2"]
    rooms = [ROOM, ROOM2, ROOM3]
    churn = Churn(env, rng, keys)
    max_queued = 0
    for _ in range(1000):
        op = rng.random()
        if op < 0.15 or not churn.open:
            churn.new_conn()
        elif op < 0.35:
            conn = rng.choice(churn.open)
            if rng.random() < 0.2:
                env.hub.handle(conn, ClientCreate(type="create", name="x", net=NET))
            else:
                join(env, conn, room_id=rng.choice(rooms))
        elif op < 0.5 and churn.tokens:
            fresh = churn.new_conn()
            if fresh is not None:
                room_id, resume_token = rng.choice(churn.tokens)
                join(env, fresh, room_id=room_id, token=resume_token)
        elif op < 0.6:
            env.hub.handle(rng.choice(churn.open), ClientLeave(type="leave"))
        elif op < 0.75:
            churn.drop(rng.choice(churn.open))
        elif op < 0.9 and churn.tokens:
            sender = rng.choice(churn.open)
            target = rng.choice(churn.tokens)[1].partition(".")[0]
            signal(env, sender, target, {"pad": "y" * rng.randrange(1, 5000)})
        else:
            clock.advance(rng.uniform(0, 6))
            env.hub.sweep()
        max_queued = max(max_queued, env.limits.queued_bytes)
        for conn in churn.open:
            churn.remember(conn)
        churn.close_replaced()
        total_seats = sum(env.limits.seats(k) for k in keys)
        assert total_seats == len(churn.open) + env.hub.stats().waiting
    while churn.open:
        churn.drop(churn.open[0])
    clock.advance(16)
    env.hub.sweep()
    assert env.hub.stats() == HubStats(0, 0, 0)
    assert [env.limits.seats(k) for k in keys] == [0, 0, 0]
    assert env.limits.queued_bytes == 0
    assert env.limits.connections == 0
    # the run went through the interesting paths
    all_joined = [m for c in churn.seen for m in c.of_type("joined")]
    assert sum(m["resumed"] for m in all_joined) > 10
    assert sum(c.closed == CLOSE_REPLACED for c in churn.seen) > 0
    assert sum(len(c.of_type("peer-left")) for c in churn.seen) > 10
    assert max_queued > 0


def test_queued_signals_from_senders_who_left_are_dropped_on_resume() -> None:
    """Signals queued by a peer that is gone by the time of the resume are not delivered:
    the addressee would start negotiating with someone who is not in its `peers`."""
    env = make_env()
    a, b, c, d = env.open(), env.open(), env.open(), env.open()
    for conn in (a, b, c, d):
        join(env, conn)
    a_id, b_id, c_id, d_id = (self_id(x) for x in (a, b, c, d))
    env.drop(d)  # the addressee waits
    signal(env, a, d_id, {"from": "a-left"})
    signal(env, b, d_id, {"from": "b-stays"})
    signal(env, c, d_id, {"from": "c-expired"})
    signal(env, b, d_id, {"from": "b-stays-2"})
    env.hub.handle(a, ClientLeave(type="leave"))  # left for good
    env.drop(c)
    # c's place is freed by the sweep while d's still waits: c's wait is made to end first.
    env.hub._rooms[ROOM].peers[c_id].disconnected_at = env.clock.now() - 15.0
    env.hub.sweep()
    assert set(env.hub._rooms[ROOM].peers) == {b_id, d_id}
    d2 = env.open()
    join(env, d2, token=token(d))
    assert joined(d2)["resumed"] is True
    assert [m["from"] for m in d2.of_type("signal")] == [b_id, b_id]
    assert signals_data(d2) == [{"from": "b-stays"}, {"from": "b-stays-2"}]
    assert env.limits.queued_bytes == 0
    assert a_id not in {m["from"] for m in d2.of_type("signal")}
    assert c_id not in {m["from"] for m in d2.of_type("signal")}


def test_queued_signal_from_a_waiting_sender_is_kept() -> None:
    env = make_env()
    a, b = env.open(), env.open()
    join(env, a)
    join(env, b)
    a_id, b_id = self_id(a), self_id(b)
    env.drop(b)
    signal(env, a, b_id, {"n": 1})
    env.drop(a)  # the sender waits too: still in the room
    b2 = env.open()
    join(env, b2, token=token(b))
    assert [m["from"] for m in b2.of_type("signal")] == [a_id]


# --- `left` is bounded; an id that fell out of it never gets a place back (L4)

MANY_JOINS = LimitConfig(joins_per_window=1_000_000)


def _churn(env: Env, conn: FakeConn, times: int) -> list[str]:
    """Join and leave `times` times; the ids that went to `left`, oldest first."""
    gone = []
    for _ in range(times):
        join(env, conn)
        gone.append(self_id(conn))
        env.hub.handle(conn, ClientLeave(type="leave"))
    return gone


def test_left_keeps_the_last_1000_ids() -> None:
    env = make_env(MANY_JOINS)
    keeper, churner = env.open(), env.open()
    join(env, keeper)
    gone = _churn(env, churner, LEFT_MAX + 5)
    room = env.hub._rooms[ROOM]
    assert LEFT_MAX == 1000
    assert len(room.left) == LEFT_MAX
    assert list(room.left) == gone[5:]


def test_evicted_id_returns_as_a_newcomer_not_with_its_old_id() -> None:
    env = make_env(MANY_JOINS)
    keeper, first = env.open(), env.open()
    join(env, keeper)
    join(env, first)
    first_id, first_token = self_id(first), token(first)
    env.hub.handle(first, ClientLeave(type="leave"))
    _churn(env, env.open(), LEFT_MAX)  # pushes first_id out of `left`
    assert first_id not in env.hub._rooms[ROOM].left
    back = env.open()
    join(env, back, token=first_token)
    assert joined(back)["resumed"] is False
    assert self_id(back) != first_id


def test_new_ids_never_repeat_an_evicted_id(monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(MANY_JOINS)
    keeper, first = env.open(), env.open()
    join(env, keeper)
    join(env, first)
    first_id, first_token = self_id(first), token(first)
    env.hub.handle(first, ClientLeave(type="leave"))
    _churn(env, env.open(), LEFT_MAX)
    real = new_peer_id
    offered = [first_id, first_id]  # the generator draws the evicted id twice
    monkeypatch.setattr(rooms_module, "new_peer_id", lambda: offered.pop() if offered else real())
    newcomer = env.open()
    join(env, newcomer)
    assert offered == []
    assert self_id(newcomer) != first_id
    # The token of the evicted id cannot take the newcomer's place either.
    back = env.open()
    join(env, back, token=first_token)
    assert self_id(back) not in {first_id, self_id(newcomer)}
    assert newcomer.closed is None


def test_room_without_evictions_still_restores_unknown_ids() -> None:
    """After a restart the room is new: a token id it never saw gets its place back."""
    env = make_env()
    a = env.open()
    join(env, a, token=issue_token(SECRET, ROOM, "abcdefgh"))
    assert (joined(a)["resumed"], self_id(a)) == (True, "abcdefgh")


def test_room_and_peer_repr_hold_no_ids_or_names() -> None:
    env = make_env(MANY_JOINS)
    a, b = env.open(), env.open()
    join(env, a, name="СекретноеИмя")
    join(env, b, name="Другое")
    _churn(env, env.open(), 2)
    room = env.hub._rooms[ROOM]
    text = repr(room) + "".join(repr(p) for p in room.peers.values())
    secrets_ = {ROOM, "СекретноеИмя", "Другое", *room.peers, *room.left}
    assert not [s for s in secrets_ if s in text]
