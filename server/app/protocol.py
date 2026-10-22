"""Signaling protocol: the source of truth for protocol/schema.json and the client."""

import math
import secrets
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)
from pydantic.alias_generators import to_camel

ID_ALPHABET: Final = "abcdefghijkmnpqrstuvwxyz23456789"
ROOM_ID_LENGTH: Final = 12
PEER_ID_LENGTH: Final = 8
ROOM_CAPACITY: Final = 5
NAME_MAX_LENGTH: Final = 32
RESUME_TOKEN_MAX_LENGTH: Final = 128
PAIR_MS_MAX: Final = 3_600_000
PAIR_RESTARTS_MAX: Final = 100

# Character class of the id patterns; matches ID_ALPHABET.
_ID_CLASS: Final = "[a-km-np-z2-9]"

type RoomId = Annotated[str, Field(pattern=f"^{_ID_CLASS}{{{ROOM_ID_LENGTH}}}$")]
type PeerId = Annotated[str, Field(pattern=f"^{_ID_CLASS}{{{PEER_ID_LENGTH}}}$")]
type Name = Annotated[
    str, Field(min_length=1, max_length=NAME_MAX_LENGTH, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$")
]
type ResumeToken = Annotated[
    str, Field(max_length=RESUME_TOKEN_MAX_LENGTH, pattern=r"^[A-Za-z0-9_.-]+$")
]
type NatType = Literal["open", "cone", "symmetric", "no-udp"]
type CandidateType = Literal["host", "srflx", "prflx", "upnp"]
type PairResult = Literal["direct", "failed"]
type IpFamily = Literal["ipv4", "ipv6"]
type ErrorCode = Literal["room-full", "bad-request"]

_CLIENT_CONFIG: Final = ConfigDict(
    alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True
)


def _require_type(schema: dict[str, Any]) -> None:
    """Server messages default `type` in Python, but the schema must require it."""
    properties = schema.get("properties", {})
    if "type" in properties:
        properties["type"].pop("default", None)
        required = schema.setdefault("required", [])
        if "type" not in required:
            required.insert(0, "type")


_SERVER_CONFIG: Final = ConfigDict(
    alias_generator=to_camel, populate_by_name=True, json_schema_extra=_require_type
)


def _reject_non_finite(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """NaN and Infinity are not JSON, and 1e400 overflows to inf; dumping them gives null.

    `allow_inf_nan=False` in the config does not reach inside JsonValue, so walk it.
    """
    stack: list[JsonValue] = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite number")
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return data


_SignalData = Annotated[dict[str, JsonValue], AfterValidator(_reject_non_finite)]


class _ClientModel(BaseModel):
    model_config = _CLIENT_CONFIG


class _ServerModel(BaseModel):
    model_config = _SERVER_CONFIG


class NetInfo(_ClientModel):
    nat: NatType | None
    upnp: bool | None
    cgnat: bool | None
    double_nat: bool | None
    vpn: bool | None


class Route(_ClientModel):
    local: CandidateType
    remote: CandidateType


class PeerInfo(_ServerModel):
    id: PeerId
    name: Name
    muted: bool


class ClientCreate(_ClientModel):
    type: Literal["create"]
    name: Name
    net: NetInfo


class ClientJoin(_ClientModel):
    type: Literal["join"]
    room_id: RoomId
    name: Name
    net: NetInfo
    resume_token: ResumeToken | None = None


class ClientSignal(_ClientModel):
    type: Literal["signal"]
    to: PeerId
    data: _SignalData


class ClientMute(_ClientModel):
    type: Literal["mute"]
    muted: bool


class ClientPairReport(_ClientModel):
    type: Literal["pair-report"]
    peer: PeerId
    result: PairResult
    route: Route | None
    family: IpFamily | None
    same_ip: bool | None
    ms: Annotated[int, Field(ge=0, le=PAIR_MS_MAX)] | None
    restarts: Annotated[int, Field(ge=0, le=PAIR_RESTARTS_MAX)]


class ClientPing(_ClientModel):
    type: Literal["ping"]


class ClientLeave(_ClientModel):
    type: Literal["leave"]


class ServerJoined(_ServerModel):
    type: Literal["joined"] = "joined"
    room_id: RoomId
    self_id: PeerId
    resumed: bool
    resume_token: ResumeToken
    peers: Annotated[list[PeerInfo], Field(max_length=ROOM_CAPACITY - 1)]


class ServerPeerJoined(_ServerModel):
    type: Literal["peer-joined"] = "peer-joined"
    id: PeerId
    name: Name


class ServerPeerLeft(_ServerModel):
    type: Literal["peer-left"] = "peer-left"
    id: PeerId


class ServerSignal(_ServerModel):
    type: Literal["signal"] = "signal"
    from_: PeerId = Field(alias="from")
    data: dict[str, JsonValue]


class ServerMute(_ServerModel):
    type: Literal["mute"] = "mute"
    id: PeerId
    muted: bool


class ServerRelay(_ServerModel):
    type: Literal["relay"] = "relay"
    a: PeerId
    b: PeerId
    via: PeerId | None


class ServerPong(_ServerModel):
    type: Literal["pong"] = "pong"


class ServerError(_ServerModel):
    type: Literal["error"] = "error"
    code: ErrorCode


type ClientMessage = Annotated[
    ClientCreate
    | ClientJoin
    | ClientSignal
    | ClientMute
    | ClientPairReport
    | ClientPing
    | ClientLeave,
    Field(discriminator="type"),
]
type ServerMessage = Annotated[
    ServerJoined
    | ServerPeerJoined
    | ServerPeerLeft
    | ServerSignal
    | ServerMute
    | ServerRelay
    | ServerPong
    | ServerError,
    Field(discriminator="type"),
]

client_message_adapter: Final[TypeAdapter[ClientMessage]] = TypeAdapter(ClientMessage)
server_message_adapter: Final[TypeAdapter[ServerMessage]] = TypeAdapter(ServerMessage)


class BadRequestError(Exception):
    """Incoming message was rejected. Carries no input data."""


def parse_client_message(raw: str | bytes) -> ClientMessage:
    try:
        return client_message_adapter.validate_json(raw)
    except ValidationError:
        pass
    # Raised after the block: else the ValidationError (holds the input, SDP) stays in __context__.
    raise BadRequestError


def dump_server_message(message: ServerMessage) -> str:
    return server_message_adapter.dump_json(message, by_alias=True).decode()


def new_room_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ROOM_ID_LENGTH))


def new_peer_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(PEER_ID_LENGTH))
