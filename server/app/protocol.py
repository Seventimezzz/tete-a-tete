"""Signaling protocol: the source of truth for protocol/schema.json and the client."""

import math
import re
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
# `data` of a signal: the object itself is depth 1, each nested object or array adds 1.
# Deeper data parses but cannot be serialized back for forwarding (about 98 levels).
MAX_SIGNAL_DATA_DEPTH: Final = 32
_DATA_TOO_DEEP: Final = "signal data is nested too deeply"


def _char_class(alphabet: str) -> str:
    """Regex class for the alphabet: runs of consecutive code points become ranges."""
    runs: list[str] = []
    for char in alphabet:
        if runs and ord(runs[-1][-1]) + 1 == ord(char):
            runs[-1] += char
        else:
            runs.append(char)
    parts = [
        re.escape(run) if len(run) == 1 else f"{re.escape(run[0])}-{re.escape(run[-1])}"
        for run in runs
    ]
    return "[" + "".join(parts) + "]"


# Character class of the id patterns, built from ID_ALPHABET.
_ID_CLASS: Final = _char_class(ID_ALPHABET)

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


def _require_type(schema: dict[str, Any]) -> None:
    """Server messages default `type` in Python, but the schema must require it."""
    properties = schema.get("properties", {})
    if "type" in properties:
        properties["type"].pop("default", None)
        required = schema.setdefault("required", [])
        if "type" not in required:
            required.insert(0, "type")


def _check_signal_data(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Limit nesting depth and reject NaN and Infinity (1e400 overflows to inf; dumping gives null).

    `allow_inf_nan=False` in the config does not reach inside JsonValue, so walk it.
    Depth: `data` itself is 1, each nested object or array adds 1.
    """
    stack: list[tuple[JsonValue, int]] = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite number")
        elif isinstance(item, dict | list):
            if depth > MAX_SIGNAL_DATA_DEPTH:
                raise ValueError(_DATA_TOO_DEEP)
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return data


_SignalData = Annotated[dict[str, JsonValue], AfterValidator(_check_signal_data)]


class _ClientModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )


class _ServerModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        json_schema_extra=_require_type,
        hide_input_in_errors=True,
    )


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
    room_id: RoomId = Field(repr=False)
    name: Name
    net: NetInfo
    resume_token: ResumeToken | None = Field(default=None, repr=False)


class ClientSignal(_ClientModel):
    type: Literal["signal"]
    to: PeerId
    data: _SignalData = Field(repr=False)


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
    room_id: RoomId = Field(repr=False)
    self_id: PeerId
    resumed: bool
    resume_token: ResumeToken = Field(repr=False)
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
    data: dict[str, JsonValue] = Field(repr=False)


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

# The union adapter has its own root config: the models' hide_input_in_errors is not enough.
_ADAPTER_CONFIG: Final = ConfigDict(hide_input_in_errors=True)
client_message_adapter: Final[TypeAdapter[ClientMessage]] = TypeAdapter(
    ClientMessage, config=_ADAPTER_CONFIG
)
server_message_adapter: Final[TypeAdapter[ServerMessage]] = TypeAdapter(
    ServerMessage, config=_ADAPTER_CONFIG
)


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
