import json
from pathlib import Path
from typing import Any

from app.protocol_schema import build_schema, write_schema

ROOT = Path(__file__).resolve().parents[2]


def _defs() -> dict[str, Any]:
    defs = build_schema()["$defs"]
    assert isinstance(defs, dict)
    return defs


def test_committed_schema_is_up_to_date() -> None:
    committed = json.loads((ROOT / "protocol" / "schema.json").read_text("utf-8"))
    assert committed == build_schema(), (
        "protocol/schema.json устарел: запустите npm run gen:protocol"
    )


def test_schema_lists_every_message() -> None:
    defs = _defs()
    client = defs["ClientMessage"]["discriminator"]["mapping"]
    server = defs["ServerMessage"]["discriminator"]["mapping"]
    assert set(client) == {"create", "join", "signal", "mute", "pair-report", "ping", "leave"}
    assert set(server) == {
        "joined",
        "peer-joined",
        "peer-left",
        "signal",
        "mute",
        "relay",
        "pong",
        "error",
    }
    for mapping in (client, server):
        for ref in mapping.values():
            assert ref.startswith("#/$defs/")
            assert "type" in defs[ref.removeprefix("#/$defs/")]["required"]


def test_schema_header() -> None:
    schema = build_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "Tete-a-tete signaling protocol"
    assert set(schema) == {"$schema", "title", "$defs"}


def test_client_models_forbid_extra_fields_and_server_models_do_not() -> None:
    defs = _defs()
    assert defs["ClientJoin"]["additionalProperties"] is False
    assert defs["NetInfo"]["additionalProperties"] is False
    assert "additionalProperties" not in defs["ServerJoined"]
    assert "additionalProperties" not in defs["PeerInfo"]


def test_named_types_are_definitions() -> None:
    assert {"RoomId", "PeerId", "Name", "ResumeToken", "NetInfo", "Route", "PeerInfo"} <= set(
        _defs()
    )


def test_write_schema_uses_utf8_and_lf(tmp_path: Path) -> None:
    target = tmp_path / "s.json"
    write_schema(target)
    data = target.read_bytes()
    assert b"\r" not in data
    assert data.endswith(b"}\n")
    assert json.loads(data.decode("utf-8")) == build_schema()
