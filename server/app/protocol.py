"""Signaling protocol: the source of truth for protocol/schema.json and the client."""

import json
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
    PrivateAttr,
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
# The largest incoming message (--ws-max-size). A forwarded signal is its frame with "to"
# renamed "from", so the server sends at most SIGNAL_FORWARD_GROWTH bytes more.
MAX_MESSAGE_BYTES: Final = 65_536
SIGNAL_FORWARD_GROWTH: Final = len('"from"') - len('"to"')
# `data` of a signal: the object itself is depth 1, each nested object or array adds 1.
# Real data is two levels deep ({description: {type, sdp}}, {candidate: {...}},
# {relayTrack: {mid, from}}); eight leaves room for growth and keeps the walk cheap.
MAX_SIGNAL_DATA_DEPTH: Final = 8
# WebSocket close codes; they are not part of the JSON schema.
CLOSE_REPLACED: Final = 4000  # the seat was taken by a new join
CLOSE_SLOW_READER: Final = 4001  # the outgoing queue overflowed
CLOSE_RATE_LIMIT: Final = 1008  # rate limit exceeded
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
type ErrorCode = Literal["room-full", "bad-request", "rate-limited"]


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
    name: Name = Field(repr=False)
    muted: bool


class ClientCreate(_ClientModel):
    type: Literal["create"]
    name: Name = Field(repr=False)
    net: NetInfo


class ClientJoin(_ClientModel):
    type: Literal["join"]
    room_id: RoomId = Field(repr=False)
    name: Name = Field(repr=False)
    net: NetInfo
    resume_token: ResumeToken | None = Field(default=None, repr=False)


class ClientSignal(_ClientModel):
    type: Literal["signal"]
    to: PeerId
    data: _SignalData = Field(repr=False)
    # JSON text of `data` exactly as it came in the frame; set by parse_client_message.
    _raw_data: str | None = PrivateAttr(default=None)


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
    name: Name = Field(repr=False)


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
        message = client_message_adapter.validate_json(raw)
    except ValidationError:
        pass
    else:
        if not isinstance(message, ClientSignal) or _keep_raw_data(message, raw):
            return message
    # Raised after the block: else the ValidationError (holds the input, SDP) stays in __context__.
    raise BadRequestError


def _keep_raw_data(message: ClientSignal, raw: str | bytes) -> bool:
    """Keep the text of `data` for forwarding. False if the forwarded signal would be longer
    than a frame of MAX_MESSAGE_BYTES gives: only if the text could not be kept and re-serializing
    grew it, or the frame itself was over the limit."""
    message._raw_data = _raw_member(raw if isinstance(raw, str) else raw.decode(), "data")
    if message._raw_data is not None and not _forwardable_data(message._raw_data):
        return False
    forwarded = dump_forwarded_signal(ID_ALPHABET[0] * PEER_ID_LENGTH, message)
    return len(forwarded.encode()) <= MAX_MESSAGE_BYTES + SIGNAL_FORWARD_GROWTH


class _DuplicateKeyError(ValueError):
    """`data` repeats a key in one object. Carries no input."""


class _NonFiniteError(ValueError):
    """`data` holds NaN, Infinity or -Infinity. Carries no input."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _DuplicateKeyError
    return result


def _reject_constant(_: str) -> Any:
    raise _NonFiniteError


def _forwardable_data(raw_data: str) -> bool:
    """The text that will be forwarded passes the checks the model ran on its own reading.

    The model keeps only the last of duplicate keys, but the raw text carries all of them: a
    duplicate could hide deep nesting or NaN from the model. Re-parse the text strictly
    (no duplicate keys, no NaN or Infinity) and check depth and numbers on that.
    """
    try:
        data = json.loads(
            raw_data, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant
        )
        _check_signal_data(data)
    except (ValueError, RecursionError):
        return False
    return True


_JSON_SPACE: Final = re.compile(r"[ \t\n\r]*")
_json_decoder: Final = json.JSONDecoder()


def _skip_space(text: str, at: int) -> int:
    match = _JSON_SPACE.match(text, at)
    return match.end() if match else at


def _raw_member(text: str, name: str) -> str | None:
    """JSON text of the last member `name` of the top-level object in `text`, or None.

    The last one, as the model keeps the last of duplicate keys. Only called on text the model
    has accepted; anything unexpected gives None, and the caller re-serializes instead.
    """
    found = None
    at = _skip_space(text, 0)
    if text[at : at + 1] != "{":
        return None
    at = _skip_space(text, at + 1)
    try:
        while text[at : at + 1] == '"':
            key, at = _json_decoder.raw_decode(text, at)  # a string: the loop checked '"'
            at = _skip_space(text, at)
            if text[at : at + 1] != ":":
                return None
            start = _skip_space(text, at + 1)
            _, at = _json_decoder.raw_decode(text, start)
            if key == name:
                found = text[start:at]
            at = _skip_space(text, at)
            if text[at : at + 1] == "}":
                return found
            if text[at : at + 1] != ",":
                return None
            at = _skip_space(text, at + 1)
    except (ValueError, RecursionError):
        return None
    return None


def dump_forwarded_signal(from_id: str, message: ClientSignal) -> str:
    """The `signal` the addressee gets. `data` keeps the text it came in: re-serializing it
    could make the message several times larger (9e15 dumps as 9000000000000000.0)."""
    raw = message._raw_data
    if raw is None:
        return dump_server_message(ServerSignal(from_=from_id, data=message.data))
    return '{"type":"signal","from":' + json.dumps(from_id) + ',"data":' + raw + "}"


def dump_server_message(message: ServerMessage) -> str:
    return server_message_adapter.dump_json(message, by_alias=True).decode()


def new_room_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ROOM_ID_LENGTH))


def new_peer_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(PEER_ID_LENGTH))
