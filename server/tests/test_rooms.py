from typing import Any

import pytest
from pydantic import JsonValue

from app.limits import LimitConfig, Limits
from app.protocol import (
    ID_ALPHABET,
    MAX_SIGNAL_DATA_DEPTH,
    ClientCreate,
    ClientJoin,
    ClientLeave,
    ClientMessage,
    ClientMute,
    ClientPairReport,
    ClientPing,
    ClientSignal,
    NetInfo,
)
from app.rooms import Hub
from app.tokens import verify_token
from tests.fakes import FakeClock, FakeConn

SECRET = b"t" * 32
NET = NetInfo(nat=None, upnp=None, cgnat=None, double_nat=None, vpn=None)
ROOM = "abcdefghjkmn"
ROOM2 = "bcdefghjkmnp"
ROOM3 = "cdefghjkmnpq"


def make_hub(config: LimitConfig | None = None) -> Hub:
    clock = FakeClock()
    return Hub(secret=SECRET, clock=clock, limits=Limits(config or LimitConfig(), clock))


def create(hub: Hub, conn: FakeConn, name: str = "Аня") -> None:
    hub.handle(conn, ClientCreate(type="create", name=name, net=NET))


def join(
    hub: Hub,
    conn: FakeConn,
    room_id: str = ROOM,
    name: str = "Аня",
    token: str | None = None,
) -> None:
    hub.handle(
        conn,
        ClientJoin(type="join", room_id=room_id, name=name, net=NET, resume_token=token),
    )


def joined(conn: FakeConn) -> dict[str, Any]:
    return conn.of_type("joined")[-1]


def error_code(conn: FakeConn) -> str:
    code: str = conn.of_type("error")[-1]["code"]
    return code


def fill_room(hub: Hub, n: int) -> list[FakeConn]:
    conns = [FakeConn(f"k{i}") for i in range(n)]
    for i, c in enumerate(conns):
        join(hub, c, name=f"p{i}")
    return conns


def test_create_returns_joined_with_new_ids_and_token() -> None:
    hub = make_hub()
    c = FakeConn()
    create(hub, c)
    j = joined(c)
    assert j["resumed"] is False
    assert j["peers"] == []
    assert len(j["roomId"]) == 12
    assert set(j["roomId"]) <= set(ID_ALPHABET)
    assert len(j["selfId"]) == 8
    assert verify_token(SECRET, j["roomId"], j["resumeToken"]) == j["selfId"]


def test_join_unknown_room_creates_it() -> None:
    hub = make_hub()
    c = FakeConn()
    join(hub, c)
    j = joined(c)
    assert j["roomId"] == ROOM
    assert j["peers"] == []
    assert j["resumed"] is False
    assert hub.stats().rooms == 1


def test_join_existing_room_notifies_others() -> None:
    hub = make_hub()
    a, b = FakeConn("a"), FakeConn("b")
    join(hub, a, name="Аня")
    join(hub, b, name="Боря")
    a_id, b_id = joined(a)["selfId"], joined(b)["selfId"]
    assert a.of_type("peer-joined") == [{"type": "peer-joined", "id": b_id, "name": "Боря"}]
    assert joined(b)["peers"] == [{"id": a_id, "name": "Аня", "muted": False}]


def test_sixth_join_gets_room_full() -> None:
    hub = make_hub()
    conns = fill_room(hub, 5)
    before = [len(c.sent) for c in conns]
    sixth = FakeConn("k6")
    join(hub, sixth)
    assert error_code(sixth) == "room-full"
    assert [len(c.sent) for c in conns] == before


def test_create_or_join_while_in_room_is_bad_request() -> None:
    hub = make_hub()
    c = FakeConn()
    join(hub, c)
    create(hub, c)
    assert error_code(c) == "bad-request"
    join(hub, c, room_id=ROOM2)
    assert len(c.of_type("error")) == 2
    assert error_code(c) == "bad-request"
    assert hub.stats().rooms == 1


def pair_report(peer: str, result: str = "failed") -> ClientPairReport:
    return ClientPairReport.model_validate(
        {
            "type": "pair-report",
            "peer": peer,
            "result": result,
            "route": None,
            "family": None,
            "sameIp": None,
            "ms": None,
            "restarts": 0,
        }
    )


@pytest.mark.parametrize("msg", ["signal", "mute", "pair-report"])
def test_room_messages_outside_room_are_bad_request(msg: str) -> None:
    hub = make_hub()
    c = FakeConn()
    messages: dict[str, ClientMessage] = {
        "signal": ClientSignal(type="signal", to="abcdefgh", data={}),
        "mute": ClientMute(type="mute", muted=True),
        "pair-report": pair_report("abcdefgh"),
    }
    hub.handle(c, messages[msg])
    assert error_code(c) == "bad-request"


def test_leave_outside_room_is_noop() -> None:
    hub = make_hub()
    c = FakeConn()
    hub.handle(c, ClientLeave(type="leave"))
    assert c.sent == []


def test_ping_gets_pong_in_any_state() -> None:
    hub = make_hub()
    c = FakeConn()
    hub.handle(c, ClientPing(type="ping"))
    join(hub, c)
    hub.handle(c, ClientPing(type="ping"))
    assert len(c.of_type("pong")) == 2


def nested(depth: int) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {}
    for _ in range(depth - 1):
        data = {"k": data}
    return data


def test_signal_forwarded_with_from_and_exact_data() -> None:
    hub = make_hub()
    a, b = FakeConn("a"), FakeConn("b")
    join(hub, a)
    join(hub, b)
    a_id, b_id = joined(a)["selfId"], joined(b)["selfId"]
    data: dict[str, JsonValue] = {
        "description": {"sdp": "привет", "n": [1, 2.5, None]},
        "deep": nested(MAX_SIGNAL_DATA_DEPTH - 1),  # data at the depth limit
    }
    hub.handle(a, ClientSignal(type="signal", to=b_id, data=data))
    assert b.of_type("signal") == [{"type": "signal", "from": a_id, "data": data}]
    assert a.of_type("signal") == []


@pytest.mark.parametrize("to", ["self", "unknown", "peer of other room"])
def test_signal_to_bad_target_is_dropped_silently(to: str) -> None:
    hub = make_hub()
    a, b, other = FakeConn("a"), FakeConn("b"), FakeConn("o")
    join(hub, a)
    join(hub, b)
    join(hub, other, room_id=ROOM2)
    target = {
        "self": joined(a)["selfId"],
        "unknown": "zzzzzzzz",
        "peer of other room": joined(other)["selfId"],
    }[to]
    before = [len(c.sent) for c in (a, b, other)]
    hub.handle(a, ClientSignal(type="signal", to=target, data={"x": 1}))
    assert [len(c.sent) for c in (a, b, other)] == before


def test_mute_broadcast_to_others_and_shown_in_later_joined() -> None:
    hub = make_hub()
    a, b, c = FakeConn("a"), FakeConn("b"), FakeConn("c")
    join(hub, a, name="Аня")
    join(hub, b, name="Боря")
    hub.handle(a, ClientMute(type="mute", muted=True))
    a_id = joined(a)["selfId"]
    assert b.of_type("mute") == [{"type": "mute", "id": a_id, "muted": True}]
    assert a.of_type("mute") == []
    join(hub, c, name="Вика")
    assert {"id": a_id, "name": "Аня", "muted": True} in joined(c)["peers"]


def test_pair_report_in_room_is_accepted_silently() -> None:
    hub = make_hub()
    a, b = FakeConn("a"), FakeConn("b")
    join(hub, a)
    join(hub, b)
    before = (len(a.sent), len(b.sent))
    hub.handle(a, pair_report(joined(b)["selfId"], "direct"))
    assert (len(a.sent), len(b.sent)) == before


def test_leave_notifies_others_and_frees_place() -> None:
    hub = make_hub()
    conns = fill_room(hub, 5)
    leaver = conns[0]
    leaver_id = joined(leaver)["selfId"]
    hub.handle(leaver, ClientLeave(type="leave"))
    for c in conns[1:]:
        assert c.of_type("peer-left") == [{"type": "peer-left", "id": leaver_id}]
    assert leaver.of_type("peer-left") == []
    sixth = FakeConn("k6")
    join(hub, sixth)
    assert sixth.of_type("error") == []
    assert len(joined(sixth)["peers"]) == 4
    # the leaver's connection stays open; the room is full again, so it is refused
    join(hub, leaver)
    assert error_code(leaver) == "room-full"


def test_last_leave_deletes_room() -> None:
    hub = make_hub()
    a = FakeConn("a")
    join(hub, a)
    hub.handle(a, ClientLeave(type="leave"))
    assert hub.stats().rooms == 0
    join(hub, a)
    assert hub.stats().rooms == 1
    assert joined(a)["peers"] == []


def test_create_limit_gives_rate_limited() -> None:
    hub = make_hub()
    c = FakeConn()
    for _ in range(10):
        create(hub, c)
        hub.handle(c, ClientLeave(type="leave"))
    create(hub, c)
    assert error_code(c) == "rate-limited"
    assert len(c.of_type("joined")) == 10


def test_join_limit_gives_rate_limited() -> None:
    hub = make_hub()
    join(hub, FakeConn("a"))  # keeps the room alive, so no creation is counted
    c = FakeConn()
    for _ in range(30):
        join(hub, c)
        hub.handle(c, ClientLeave(type="leave"))
    join(hub, c)
    assert error_code(c) == "rate-limited"
    assert len(c.of_type("joined")) == 30


def test_global_room_limit() -> None:
    hub = make_hub(LimitConfig(max_rooms=2))
    a, b, c = FakeConn("a"), FakeConn("b"), FakeConn("c")
    join(hub, a, room_id=ROOM)
    join(hub, b, room_id=ROOM2)
    join(hub, c, room_id=ROOM3)
    assert error_code(c) == "rate-limited"
    # joining an existing room is still allowed
    d = FakeConn("d")
    join(hub, d, room_id=ROOM)
    assert d.of_type("error") == []


def test_state_repr_has_no_name_or_data() -> None:
    hub = make_hub()
    a, b = FakeConn("a"), FakeConn("b")
    join(hub, a, name="СекретноеИмя")
    join(hub, b, name="Другое")
    hub.handle(a, ClientSignal(type="signal", to=joined(b)["selfId"], data={"sdp": "ЦЕЛЬ"}))
    text = repr(hub._rooms)
    assert text.count("Room(") == 1
    assert "Peer(" in text
    for secret in ("СекретноеИмя", "Другое", "ЦЕЛЬ", "NetInfo", "FakeConn"):
        assert secret not in text


def test_disconnected_releases_seat() -> None:
    clock = FakeClock()
    limits = Limits(LimitConfig(), clock)
    hub = Hub(secret=SECRET, clock=clock, limits=limits)
    assert limits.try_open("k")
    hub.disconnected(FakeConn("k"))
    assert limits.seats("k") == 0
