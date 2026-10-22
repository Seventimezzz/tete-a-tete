"""QA tests for the protocol: server/client agreement, privacy of BadRequestError, odd input.

The shared fixture `fixtures/agreement_cases.json` is read by this file and by
`client/src/protocol.agreement.test.ts`: the same raw text goes to Pydantic and to zod.
"""

import json
import logging
import secrets
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app import protocol
from app.protocol import (
    ID_ALPHABET,
    ROOM_CAPACITY,
    BadRequestError,
    ClientSignal,
    PeerInfo,
    ServerJoined,
    ServerSignal,
    dump_server_message,
    new_peer_id,
    new_room_id,
    parse_client_message,
    server_message_adapter,
)
from app.protocol_schema import build_schema

FIXTURE = Path(__file__).parent / "fixtures" / "agreement_cases.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))

NET = '{"nat":null,"upnp":null,"cgnat":null,"doubleNat":null,"vpn":null}'
SECRETS = ["SECRET-SDP-v=0-192.0.2.77", "candidate:1 1 udp 2122260223 198.51.100.9 54321 typ host"]


def accepted_by_pydantic(case: dict[str, Any]) -> bool:
    try:
        if case["dir"] == "client":
            parse_client_message(case["raw"])
        else:
            server_message_adapter.validate_json(case["raw"])
    except (BadRequestError, ValidationError):
        return False
    return True


def agreement_params() -> list[Any]:
    params = []
    for index, case in enumerate(CASES):
        marks = []
        bug = case.get("bug", {}).get("py")
        if bug:
            marks.append(
                pytest.mark.xfail(
                    strict=True, reason=f"{bug}: Pydantic disagrees with the protocol"
                )
            )
        params.append(pytest.param(case, id=f"{index}-{case['dir']}-{case['label']}", marks=marks))
    return params


@pytest.mark.parametrize("case", agreement_params())
def test_pydantic_verdict_matches_shared_fixture(case: dict[str, Any]) -> None:
    assert accepted_by_pydantic(case) == (case["expect"] == "accept")


def test_fixture_covers_both_directions_and_both_verdicts() -> None:
    assert {c["dir"] for c in CASES} == {"client", "server"}
    assert {c["expect"] for c in CASES} == {"accept", "reject"}


def test_signal_data_with_nan_is_not_forwarded_altered() -> None:
    raw = '{"type":"signal","to":"abcdefgh","data":{"a":NaN}}'
    try:
        parsed = parse_client_message(raw)
    except BadRequestError:
        return
    assert isinstance(parsed, ClientSignal)
    forwarded = ServerSignal(from_="abcdefgh", data=parsed.data)  # type: ignore[call-arg]
    out = json.loads(dump_server_message(forwarded))
    assert out["data"]["a"] is not None


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type":"create","name":"\xff","net":' + NET.encode() + b"}",
        b"\xef\xbb\xbf" + b'{"type":"ping"}',
        '{"type":"ping"}'.encode("utf-16"),
        b"",
        b"\x00\x00",
        b"[" * 100_000 + b"]" * 100_000,
        b'{"type":"signal","to":"abcdefgh","data":{"a":' + b"[" * 100_000 + b"]" * 100_000 + b"}}",
        b'{"type":"signal","to":"abcdefgh","data":{"a":' + b"9" * 5000 + b"}}",
    ],
    ids=[
        "invalid-utf8",
        "utf8-bom",
        "utf16",
        "empty",
        "nul-bytes",
        "deep-top-array",
        "deep-data",
        "huge-int",
    ],
)
def test_hostile_bytes_give_bad_request_not_a_crash(raw: bytes) -> None:
    with pytest.raises(BadRequestError):
        parse_client_message(raw)


def test_deep_but_legal_data_is_accepted() -> None:
    raw = '{"type":"signal","to":"abcdefgh","data":{"a":' + "[" * 100 + "]" * 100 + "}}"
    assert isinstance(parse_client_message(raw), ClientSignal)


def test_oversized_name_is_rejected_without_crash() -> None:
    raw = '{"type":"create","name":"' + "a" * 1_000_000 + '","net":' + NET + "}"
    with pytest.raises(BadRequestError):
        parse_client_message(raw)


def test_camel_case_key_wins_over_conflicting_snake_case_key() -> None:
    raw = (
        '{"type":"join","roomId":"abcdefghijkm","room_id":"zzzzzzzzzzzz","name":"A","net":'
        + NET
        + "}"
    )
    parsed = parse_client_message(raw)
    assert parsed.model_dump()["room_id"] == "abcdefghijkm"


def test_bad_camel_case_key_is_not_rescued_by_good_snake_case_key() -> None:
    raw = '{"type":"join","roomId":"bad","room_id":"abcdefghijkm","name":"A","net":' + NET + "}"
    with pytest.raises(BadRequestError):
        parse_client_message(raw)


def _flatten(error: BaseException) -> str:
    parts = [str(error), repr(error), repr(error.args)]
    parts += traceback.format_exception(error)
    seen: list[BaseException] = []
    link: BaseException | None = error
    while link is not None and link not in seen:
        seen.append(link)
        parts.append(repr(link))
        parts.append(repr(getattr(link, "__notes__", None)))
        link = link.__cause__ or link.__context__
    return "\n".join(parts)


def _hostile_inputs() -> dict[str, str]:
    sdp, cand = SECRETS
    return {
        "bad-type-in-signal": json.dumps(
            {"type": "signal", "to": "bad", "data": {"description": {"sdp": sdp}}}
        ),
        "signal-data-not-object": json.dumps({"type": "signal", "to": "abcdefgh", "data": sdp}),
        "extra-field-with-candidate": json.dumps({"type": "ping", "candidate": cand}),
        "unknown-type": json.dumps({"type": sdp}),
        "type-is-candidate": json.dumps({"type": cand, "data": {"sdp": sdp}}),
        "bad-room-id": json.dumps(
            {"type": "join", "roomId": cand, "name": sdp, "net": NET, "resumeToken": sdp}
        ),
        "bad-name": json.dumps({"type": "create", "name": sdp + "\n", "net": {}}),
        "bad-resume-token": json.dumps(
            {"type": "join", "roomId": "abcdefghijkm", "name": "A", "net": {}, "resumeToken": sdp}
        ),
        "truncated-json": '{"type":"signal","data":{"sdp":"' + sdp,
        "json-array": json.dumps([sdp, cand]),
        "json-string": json.dumps(sdp),
        "trailing-garbage": '{"type":"ping"} ' + cand,
        "pair-report-bad": json.dumps(
            {"type": "pair-report", "peer": cand, "result": sdp, "ms": cand}
        ),
    }


@pytest.mark.parametrize("name", sorted(_hostile_inputs()))
def test_bad_request_carries_no_input_in_text_chain_or_traceback(name: str) -> None:
    raw = _hostile_inputs()[name]
    with pytest.raises(BadRequestError) as info:
        parse_client_message(raw)
    text = _flatten(info.value)
    for secret in SECRETS:
        assert secret not in text
    assert "192.0.2.77" not in text
    assert "198.51.100.9" not in text
    assert info.value.args == ()
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


@pytest.mark.parametrize("name", sorted(_hostile_inputs()))
def test_parsing_bad_messages_writes_nothing_to_logs(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    raw = _hostile_inputs()[name]
    with caplog.at_level(logging.DEBUG), pytest.raises(BadRequestError):
        parse_client_message(raw)
    assert caplog.records == []


def test_parsing_bytes_with_secrets_writes_nothing_to_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = b'{"type":"signal","to":"x","data":{"sdp":"' + SECRETS[0].encode() + b'"}}\xff'
    with caplog.at_level(logging.DEBUG), pytest.raises(BadRequestError) as info:
        parse_client_message(raw)
    assert caplog.records == []
    assert "192.0.2.77" not in _flatten(info.value)


def test_valid_signal_with_secrets_is_parsed_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = json.dumps({"type": "signal", "to": "abcdefgh", "data": {"description": SECRETS[0]}})
    with caplog.at_level(logging.DEBUG):
        parse_client_message(raw)
    assert caplog.records == []


def _public_models() -> list[type[BaseModel]]:
    return [
        obj
        for name, obj in vars(protocol).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and not name.startswith("_")
    ]


@pytest.mark.parametrize("model", _public_models(), ids=lambda m: m.__name__)
def test_schema_properties_and_required_match_the_model(model: type[BaseModel]) -> None:
    definition = build_schema()["$defs"][model.__name__]
    expected_keys = {f.alias or name for name, f in model.model_fields.items()}
    expected_required = {
        f.alias or name for name, f in model.model_fields.items() if f.is_required()
    }
    if "type" in expected_keys:
        expected_required.add("type")
        assert "default" not in definition["properties"]["type"]
    assert set(definition["properties"]) == expected_keys
    assert set(definition.get("required", [])) == expected_required


def test_joined_accepts_four_peers_and_refuses_a_fifth() -> None:
    def peers(n: int) -> list[PeerInfo]:
        ids = ["bcdefghj", "cdefghjk", "defghjkm", "efghjkmn", "fghjkmnp"]
        return [PeerInfo(id=ids[i], name="B", muted=False) for i in range(n)]

    def make(n: int) -> ServerJoined:
        return ServerJoined(
            room_id="abcdefghijkm",
            self_id="abcdefgh",
            resumed=False,
            resume_token="t",  # noqa: S106
            peers=peers(n),
        )

    assert len(make(ROOM_CAPACITY - 1).peers) == 4
    with pytest.raises(ValidationError):
        make(ROOM_CAPACITY)


def test_id_alphabet_has_no_lookalikes_and_32_symbols() -> None:
    assert len(ID_ALPHABET) == 32
    assert len(set(ID_ALPHABET)) == 32
    assert not set("lo01") & set(ID_ALPHABET)


def test_every_alphabet_symbol_appears_in_every_position_of_generated_ids() -> None:
    rooms = [new_room_id() for _ in range(20_000)]
    peers = [new_peer_id() for _ in range(20_000)]
    for position in range(12):
        assert {r[position] for r in rooms} == set(ID_ALPHABET)
    for position in range(8):
        assert {p[position] for p in peers} == set(ID_ALPHABET)


def test_generated_ids_are_not_biased_toward_any_symbol() -> None:
    counts = Counter("".join(new_room_id() for _ in range(20_000)))
    expected = 20_000 * 12 / 32
    assert all(0.9 * expected < counts[ch] < 1.1 * expected for ch in ID_ALPHABET)


def test_ids_come_from_a_cryptographic_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = secrets.choice

    def spy(seq: Any) -> Any:
        calls.append(len(seq))
        return real(seq)

    monkeypatch.setattr(secrets, "choice", spy)
    new_room_id()
    new_peer_id()
    assert calls == [32] * 20
