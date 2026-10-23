import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.protocol import (
    _ID_CLASS,
    ID_ALPHABET,
    MAX_MESSAGE_BYTES,
    MAX_SIGNAL_DATA_DEPTH,
    SIGNAL_FORWARD_GROWTH,
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
    client_message_adapter,
    dump_forwarded_signal,
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


@pytest.mark.parametrize("message", CLIENT_VALID, ids=type_ids)
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
    message = ServerSignal(from_=PEER, data={"x": 1})
    signal = json.loads(dump_server_message(message))
    assert signal == {"type": "signal", "from": PEER, "data": {"x": 1}}


@pytest.mark.parametrize("message", SERVER_VALID, ids=type_ids)
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


def _objects(depth: int) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for _ in range(depth - 1):
        data = {"a": data}
    return data


def _arrays(depth: int) -> dict[str, Any]:
    inner: Any = []
    for _ in range(depth - 2):
        inner = [inner]
    return {"a": inner}


@pytest.mark.parametrize("build", [_objects, _arrays], ids=["objects", "arrays"])
def test_signal_data_depth_boundary(build: Any) -> None:
    def raw(depth: int) -> str:
        return json.dumps({"type": "signal", "to": PEER, "data": build(depth)})

    parsed = parse_client_message(raw(MAX_SIGNAL_DATA_DEPTH))
    assert isinstance(parsed, ClientSignal)
    forwarded = ServerSignal(from_=PEER, data=parsed.data)
    assert json.loads(dump_server_message(forwarded))["data"] == build(MAX_SIGNAL_DATA_DEPTH)
    with pytest.raises(BadRequestError) as info:
        parse_client_message(raw(MAX_SIGNAL_DATA_DEPTH + 1))
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_id_class_matches_exactly_the_alphabet() -> None:
    for code in range(128):
        char = chr(code)
        assert bool(re.fullmatch(_ID_CLASS, char)) == (char in ID_ALPHABET)
    assert _ID_CLASS == "[a-km-np-z2-9]"


SECRET_SDP = "v=0 SECRET-SDP 192.0.2.77"  # noqa: S105
SECRET_TOKEN = "SECRET-TOKEN.abc"  # noqa: S105


def test_server_validation_error_hides_the_input() -> None:
    with pytest.raises(ValidationError) as info:
        ServerJoined(
            room_id="bad room",
            self_id=PEER,
            resumed=False,
            resume_token=SECRET_TOKEN + "!",
            peers=[],
        )
    with pytest.raises(ValidationError) as signal_info:
        server_message_adapter.validate_python(
            {"type": "signal", "from": "bad", "data": SECRET_SDP}
        )
    for error in (info.value, signal_info.value):
        text = str(error) + repr(error)
        assert SECRET_TOKEN not in text
        assert "bad room" not in text
        assert "192.0.2.77" not in text


def test_repr_and_str_of_models_hide_secrets() -> None:
    net = {"nat": None, "upnp": None, "cgnat": None, "doubleNat": None, "vpn": None}
    signal = parse_client_message(
        json.dumps({"type": "signal", "to": PEER, "data": {"description": SECRET_SDP}})
    )
    join = parse_client_message(
        json.dumps(
            {"type": "join", "roomId": ROOM, "name": "A", "net": net, "resumeToken": SECRET_TOKEN}
        )
    )
    joined = ServerJoined(
        room_id=ROOM, self_id=PEER, resumed=True, resume_token=SECRET_TOKEN, peers=[]
    )
    forwarded = ServerSignal(from_=PEER, data={"description": SECRET_SDP})
    for message in (signal, join, joined, forwarded):
        text = repr(message) + str(message)
        assert SECRET_SDP not in text
        assert SECRET_TOKEN not in text
        assert ROOM not in text


def test_client_adapter_validation_error_hides_the_input() -> None:
    with pytest.raises(ValidationError) as info:
        client_message_adapter.validate_python({"type": "signal", "to": "bad", "data": SECRET_SDP})
    assert "192.0.2.77" not in str(info.value) + repr(info.value)


# --- forwarding a signal: the raw text of `data` (QA-3)

NEWLINE = chr(10)


def _forward(frame: str) -> str:
    message = parse_client_message(frame)
    assert isinstance(message, ClientSignal)
    return dump_forwarded_signal(PEER, message)


def test_forwarded_signal_keeps_the_raw_data_text() -> None:
    raw_data = '{ "candidate" : [9e15, 1E5, -0, 0.10] ,' + NEWLINE + r' "s": "\u0041\/\u2028"}'
    frame = f'{{ "type": "signal",\t"to": "{PEER}", "data": {raw_data} }}'
    text = _forward(frame)
    assert text == f'{{"type":"signal","from":"{PEER}","data":{raw_data}}}'
    assert server_message_adapter.validate_json(text) == ServerSignal(
        from_=PEER, data=json.loads(raw_data)
    )


def test_forwarded_signal_takes_the_data_member_the_model_validated() -> None:
    """Duplicate keys: the model keeps the last one, forwarding must take the same text."""
    frame = (
        rf'{{"data":{{"first":1}},"type":"signal","d\u0061ta":{{"last":[1e1]}},'
        f'"to":"{PEER}","data" :{{"last":[2e2]}}}}'
    )
    message = parse_client_message(frame)
    assert isinstance(message, ClientSignal)
    assert message.data == {"last": [200.0]}
    assert _forward(frame) == f'{{"type":"signal","from":"{PEER}","data":{{"last":[2e2]}}}}'


def test_forwarded_signal_built_in_python_is_dumped() -> None:
    message = ClientSignal(type="signal", to=PEER, data={"sdp": "v=0", "n": [1, 2.5]})
    assert dump_forwarded_signal("bcdefghj", message) == dump_server_message(
        ServerSignal(from_="bcdefghj", data={"sdp": "v=0", "n": [1, 2.5]})
    )


def test_forwarded_signal_is_at_most_two_bytes_longer_than_the_frame() -> None:
    """The key "to" becomes "from": the only growth, whatever the frame looks like."""
    numbers = ",".join(["9e15"] * 13_000)
    frame = f'{{"type":"signal","to":"{PEER}","data":{{"candidate":[{numbers}]}}}}'
    assert len(_forward(frame).encode()) == len(frame.encode()) + 2


def _signal_frame_of_size(total: int) -> str:
    head = f'{{"type":"signal","to":"{PEER}","data":{{"sdp":"'
    frame = head + "s" * (total - len(head) - len('"}}')) + '"}}'
    assert len(frame.encode()) == total
    return frame


def test_signal_frame_at_the_limit_is_forwarded_two_bytes_longer() -> None:
    """uvicorn lets in 65 536 bytes; "to" -> "from" adds two, and that is all it adds."""
    text = _forward(_signal_frame_of_size(MAX_MESSAGE_BYTES))
    assert len(text.encode()) == MAX_MESSAGE_BYTES + SIGNAL_FORWARD_GROWTH == 65_538


def test_signal_that_would_be_forwarded_over_the_bound_is_bad_request() -> None:
    """Only a frame over --ws-max-size gets here, but parsing does not rely on uvicorn."""
    with pytest.raises(BadRequestError):
        parse_client_message(_signal_frame_of_size(MAX_MESSAGE_BYTES + 1))
