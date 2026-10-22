import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.protocol import (
    ID_ALPHABET,
    BadRequestError,
    ClientCreate,
    ClientJoin,
    ClientLeave,
    ClientMute,
    ClientPairReport,
    ClientPing,
    ClientSignal,
    PeerInfo,
    ServerJoined,
    ServerRelay,
    ServerSignal,
    dump_server_message,
    new_peer_id,
    new_room_id,
    parse_client_message,
    server_message_adapter,
)

EXAMPLES = json.loads(
    (Path(__file__).parents[2] / "protocol" / "examples.json").read_text(encoding="utf-8")
)
CLIENT_VALID: list[dict[str, Any]] = EXAMPLES["client"]["valid"]
CLIENT_INVALID: list[dict[str, Any]] = EXAMPLES["client"]["invalid"]
SERVER_VALID: list[dict[str, Any]] = EXAMPLES["server"]["valid"]
SERVER_INVALID: list[dict[str, Any]] = EXAMPLES["server"]["invalid"]

CLASS_BY_TYPE: dict[str, type] = {
    "create": ClientCreate,
    "join": ClientJoin,
    "signal": ClientSignal,
    "mute": ClientMute,
    "pair-report": ClientPairReport,
    "ping": ClientPing,
    "leave": ClientLeave,
}

ROOM = "abcdefghijkm"
PEER = "abcdefgh"


def type_ids(message: dict[str, Any]) -> str:
    return str(message["type"])


@pytest.mark.parametrize("message", CLIENT_VALID, ids=range(len(CLIENT_VALID)))
def test_valid_client_example_parses_to_its_class(message: dict[str, Any]) -> None:
    parsed = parse_client_message(json.dumps(message))
    assert isinstance(parsed, CLASS_BY_TYPE[message["type"]])


def test_client_examples_cover_every_client_type() -> None:
    assert {m["type"] for m in CLIENT_VALID} == set(CLASS_BY_TYPE)


@pytest.mark.parametrize("case", CLIENT_INVALID, ids=lambda c: c["why"])
def test_invalid_client_example_is_bad_request(case: dict[str, Any]) -> None:
    with pytest.raises(BadRequestError):
        parse_client_message(json.dumps(case["message"]))


@pytest.mark.parametrize(
    "raw", ["", "{", "[]", "42", "null", '"ping"', '{"type": "ping"', b"\xff\xfe\x00"]
)
def test_malformed_input_is_bad_request(raw: str | bytes) -> None:
    with pytest.raises(BadRequestError):
        parse_client_message(raw)


def test_deeply_nested_signal_data_is_bad_request() -> None:
    raw = '{"type":"signal","to":"abcdefgh","data":' + '{"a":' * 10_000 + "1" + "}" * 10_000 + "}"
    assert len(raw) < 65_536
    with pytest.raises(BadRequestError):
        parse_client_message(raw)


def test_signal_with_large_sdp_parses() -> None:
    data = {"description": {"type": "offer", "sdp": "v=0\r\n" + "a" * 60_000}}
    raw = json.dumps({"type": "signal", "to": PEER, "data": data})
    assert len(raw) < 65_536
    parsed = parse_client_message(raw)
    assert isinstance(parsed, ClientSignal)
    assert parsed.data == data


def test_bad_request_carries_no_input() -> None:
    raw = json.dumps(
        {
            "type": "signal",
            "to": "bad",
            "data": {"description": {"type": "offer", "sdp": "v=0 SECRET-SDP 192.0.2.1"}},
        }
    )
    with pytest.raises(BadRequestError) as info:
        parse_client_message(raw)
    e = info.value
    for text in (str(e), repr(e), repr(e.args)):
        assert "SECRET-SDP" not in text
        assert "192.0.2.1" not in text
    assert e.__cause__ is None
    assert e.__context__ is None


def test_server_messages_serialize_by_alias() -> None:
    joined = ServerJoined(
        room_id=ROOM,
        self_id=PEER,
        resumed=False,
        resume_token="t.x",  # noqa: S106
        peers=[PeerInfo(id="mnpqrstu", name="Аня", muted=True)],
    )
    assert json.loads(dump_server_message(joined)) == {
        "type": "joined",
        "roomId": ROOM,
        "selfId": PEER,
        "resumed": False,
        "resumeToken": "t.x",
        "peers": [{"id": "mnpqrstu", "name": "Аня", "muted": True}],
    }
    relay = json.loads(dump_server_message(ServerRelay(a=PEER, b="mnpqrstu", via=None)))
    assert "via" in relay
    assert relay["via"] is None
    message = ServerSignal(from_=PEER, data={"x": 1})  # type: ignore[call-arg]
    signal = json.loads(dump_server_message(message))
    assert signal == {"type": "signal", "from": PEER, "data": {"x": 1}}


@pytest.mark.parametrize("message", SERVER_VALID, ids=range(len(SERVER_VALID)))
def test_valid_server_example_round_trips(message: dict[str, Any]) -> None:
    parsed = server_message_adapter.validate_python(message)
    assert json.loads(dump_server_message(parsed)) == message


@pytest.mark.parametrize("case", SERVER_INVALID, ids=lambda c: c["why"])
def test_invalid_server_example_is_rejected(case: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        server_message_adapter.validate_python(case["message"])


@pytest.mark.parametrize(("generate", "length"), [(new_room_id, 12), (new_peer_id, 8)])
def test_generated_ids_have_format_and_do_not_repeat(generate: Any, length: int) -> None:
    ids = [generate() for _ in range(10_000)]
    pattern = re.compile(f"[{ID_ALPHABET}]{{{length}}}")
    assert all(pattern.fullmatch(i) for i in ids)
    assert len(set(ids)) == 10_000


def test_generated_room_id_is_accepted_in_join() -> None:
    raw = json.dumps(
        {
            "type": "join",
            "roomId": new_room_id(),
            "name": "Аня",
            "net": {"nat": None, "upnp": None, "cgnat": None, "doubleNat": None, "vpn": None},
        }
    )
    assert isinstance(parse_client_message(raw), ClientJoin)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
@pytest.mark.parametrize("shape", ["{}", "[{}]", '{{"x":[1,{}]}}'])
def test_signal_data_rejects_non_finite_numbers_at_any_depth(number: str, shape: str) -> None:
    data = '{"a":' + shape.format(number) + "}"
    raw = '{"type":"signal","to":"abcdefgh","data":' + data + "}"
    with pytest.raises(BadRequestError) as info:
        parse_client_message(raw)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_signal_data_keeps_finite_numbers() -> None:
    raw = '{"type":"signal","to":"abcdefgh","data":{"a":[1.5,-2e10,{"b":0.0}]}}'
    parsed = parse_client_message(raw)
    assert isinstance(parsed, ClientSignal)
