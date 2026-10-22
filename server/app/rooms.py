"""Rooms in memory. Pure synchronous logic: no network, no asyncio, time from the clock.

Nothing here may log or put into a repr names, signal data, ids or tokens.
"""

from dataclasses import dataclass, field
from typing import Final, Protocol

from app.clock import Clock
from app.limits import Limits
from app.protocol import (
    ROOM_CAPACITY,
    ClientCreate,
    ClientJoin,
    ClientLeave,
    ClientMessage,
    ClientMute,
    ClientPairReport,
    ClientPing,
    ClientSignal,
    NetInfo,
    PeerInfo,
    ServerError,
    ServerJoined,
    ServerMessage,
    ServerMute,
    ServerPeerJoined,
    ServerPeerLeft,
    ServerPong,
    ServerSignal,
    dump_server_message,
    new_peer_id,
    new_room_id,
)
from app.tokens import issue_token, verify_token

WAIT_SECONDS: Final = 15.0
WAIT_QUEUE_MAX: Final = 50
WAIT_QUEUE_BYTES: Final = 262_144
WAIT_QUEUE_PER_SENDER: Final = 25


class Conn(Protocol):
    """A client connection. Compared by identity."""

    @property
    def key(self) -> str: ...

    def send(self, text: str) -> None:
        """Queue ready JSON for the connection; never blocks."""

    def close(self, code: int) -> None: ...


@dataclass(eq=False)
class Peer:
    id: str
    conn: Conn | None = field(repr=False)  # None while waiting for a return
    name: str = field(repr=False)
    muted: bool
    net: NetInfo = field(repr=False)
    joined_at: float
    disconnected_at: float | None = None
    queue: list[str] = field(default_factory=list, repr=False)  # signals while waiting
    seat_key: str | None = field(default=None, repr=False)  # IP key whose slot a waiting seat holds


@dataclass(eq=False)
class Room:
    id: str = field(repr=False)
    peers: dict[str, Peer] = field(default_factory=dict)
    left: set[str] = field(default_factory=set)  # seat freed: a return gets resumed=False


@dataclass(frozen=True)
class HubStats:
    rooms: int
    peers: int
    waiting: int


def _text(message: ServerMessage) -> str:
    return dump_server_message(message)


class Hub:
    def __init__(self, *, secret: bytes, clock: Clock, limits: Limits) -> None:
        self._secret = secret
        self._clock = clock
        self._limits = limits
        self._rooms: dict[str, Room] = {}
        self._seats: dict[int, tuple[str, str]] = {}  # id(conn) -> (room id, peer id)

    def __repr__(self) -> str:
        return f"Hub(rooms={len(self._rooms)})"

    def handle(self, conn: Conn, message: ClientMessage) -> None:
        match message:
            case ClientPing():
                conn.send(_text(ServerPong()))
            case ClientCreate():
                self._create(conn, message)
            case ClientJoin():
                self._join(conn, message)
            case ClientLeave():
                self._leave(conn)
            case ClientSignal():
                self._signal(conn, message)
            case ClientMute():
                self._mute(conn, message)
            case ClientPairReport():
                # Statistics arrive in stage 6; the report must still come from inside a room.
                if self._locate(conn) is None:
                    self._error(conn, "bad-request")

    def disconnected(self, conn: Conn) -> None:
        """Stub: task 5 adds the waiting period. For now only the slot is released."""
        self._limits.release_seat(conn.key)

    def sweep(self) -> None:
        """Task 5."""

    def stats(self) -> HubStats:
        peers = [p for room in self._rooms.values() for p in room.peers.values()]
        return HubStats(
            rooms=len(self._rooms),
            peers=len(peers),
            waiting=sum(1 for p in peers if p.conn is None),
        )

    # --- helpers

    def _error(self, conn: Conn, code: str) -> None:
        conn.send(_text(ServerError.model_validate({"code": code})))

    def _locate(self, conn: Conn) -> tuple[Room, Peer] | None:
        seat = self._seats.get(id(conn))
        if seat is None:
            return None
        room = self._rooms.get(seat[0])
        peer = room.peers.get(seat[1]) if room else None
        if room is None or peer is None or peer.conn is not conn:
            return None
        return room, peer

    def _broadcast(self, room: Room, message: ServerMessage, *, skip: Peer) -> None:
        text = _text(message)
        for other in room.peers.values():
            if other is not skip and other.conn is not None:
                other.conn.send(text)

    # --- create / join

    def _create(self, conn: Conn, message: ClientCreate) -> None:
        if self._locate(conn) is not None:
            return self._error(conn, "bad-request")
        if not self._limits.try_join(conn.key):
            return self._error(conn, "rate-limited")
        self._new_room(conn, message.name, message.net)

    def _join(self, conn: Conn, message: ClientJoin) -> None:
        if self._locate(conn) is not None:
            return self._error(conn, "bad-request")
        if not self._limits.try_join(conn.key):
            return self._error(conn, "rate-limited")
        # Task 5 completes the table: a valid token is verified here but, for now,
        # behaves like an absent one.
        if message.resume_token is not None:
            verify_token(self._secret, message.room_id, message.resume_token)
        room = self._rooms.get(message.room_id)
        if room is None:
            self._new_room(conn, message.name, message.net, room_id=message.room_id)
        elif len(room.peers) >= ROOM_CAPACITY:
            self._error(conn, "room-full")
        else:
            self._add_peer(conn, message.room_id, room, message.name, message.net)

    def _new_room(self, conn: Conn, name: str, net: NetInfo, *, room_id: str | None = None) -> None:
        if len(self._rooms) >= self._limits.config.max_rooms or not self._limits.try_create(
            conn.key
        ):
            return self._error(conn, "rate-limited")
        if room_id is None:
            room_id = new_room_id()
            while room_id in self._rooms:
                room_id = new_room_id()
        room = self._rooms[room_id] = Room(id=room_id)
        self._add_peer(conn, room_id, room, name, net)

    def _add_peer(self, conn: Conn, room_id: str, room: Room, name: str, net: NetInfo) -> None:
        peer_id = new_peer_id()
        while peer_id in room.peers or peer_id in room.left:
            peer_id = new_peer_id()
        peer = Peer(
            id=peer_id,
            conn=conn,
            name=name,
            muted=False,
            net=net,
            joined_at=self._clock.now(),
        )
        others = [PeerInfo(id=p.id, name=p.name, muted=p.muted) for p in room.peers.values()]
        room.peers[peer_id] = peer
        self._seats[id(conn)] = (room_id, peer_id)
        self._broadcast(room, ServerPeerJoined(id=peer_id, name=name), skip=peer)
        conn.send(
            _text(
                ServerJoined(
                    room_id=room_id,
                    self_id=peer_id,
                    resumed=False,
                    resume_token=issue_token(self._secret, room_id, peer_id),
                    peers=others,
                )
            )
        )

    # --- in-room messages

    def _leave(self, conn: Conn) -> None:
        located = self._locate(conn)
        if located is None:
            return
        room, peer = located
        self._remove_peer(room, peer)

    def _remove_peer(self, room: Room, peer: Peer) -> None:
        """Remove a peer for good: free the seat, drop its queue, tell the others."""
        del room.peers[peer.id]
        room.left.add(peer.id)
        peer.queue.clear()
        if peer.conn is not None:
            self._seats.pop(id(peer.conn), None)
        if room.peers:
            self._broadcast(room, ServerPeerLeft(id=peer.id), skip=peer)
        else:
            self._delete_room(room)

    def _delete_room(self, room: Room) -> None:
        del self._rooms[room.id]

    def _signal(self, conn: Conn, message: ClientSignal) -> None:
        located = self._locate(conn)
        if located is None:
            return self._error(conn, "bad-request")
        room, sender = located
        target = room.peers.get(message.to)
        if target is None or target is sender:
            return  # the addressee may have just left
        if target.conn is not None:
            target.conn.send(_text(ServerSignal(from_=sender.id, data=message.data)))
        # Waiting addressee: the queue is task 5.

    def _mute(self, conn: Conn, message: ClientMute) -> None:
        located = self._locate(conn)
        if located is None:
            return self._error(conn, "bad-request")
        room, peer = located
        peer.muted = message.muted
        self._broadcast(room, ServerMute(id=peer.id, muted=message.muted), skip=peer)
