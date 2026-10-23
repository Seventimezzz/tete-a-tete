"""Rooms in memory. Pure synchronous logic: no network, no asyncio, time from the clock.

Nothing here logs. Reprs of Hub, Room and Peer hold counts and times only: no room or peer
ids, names, signal data or tokens.
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Final, Protocol

from app.clock import Clock
from app.limits import Limits
from app.protocol import (
    CLOSE_REPLACED,
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
    dump_forwarded_signal,
    dump_server_message,
    new_peer_id,
    new_room_id,
)
from app.tokens import issue_token, verify_token

WAIT_SECONDS: Final = 15.0
WAIT_QUEUE_MAX: Final = 50
WAIT_QUEUE_BYTES: Final = 262_144
WAIT_QUEUE_PER_SENDER: Final = 25
# Freed ids a room remembers exactly; older ones go to a bitmap that only grows.
LEFT_MAX: Final = 1000
_EVICTED_BITS: Final = 1 << 16
# Draws of a new peer id before giving up (only a room with a saturated bitmap gets there).
_NEW_ID_TRIES: Final = 64


class Conn(Protocol):
    """A client connection. Compared by identity."""

    @property
    def key(self) -> str: ...

    def send(self, text: str) -> None:
        """Queue ready JSON for the connection; never blocks."""

    def close(self, code: int) -> None: ...


@dataclass(eq=False)
class Peer:
    id: str = field(repr=False)
    conn: Conn | None = field(repr=False)  # None while waiting for a return
    name: str = field(repr=False)
    muted: bool
    net: NetInfo = field(repr=False)
    joined_at: float
    disconnected_at: float | None = None
    # Signals while waiting: (sender id, ready JSON).
    queue: list[tuple[str, str]] = field(default_factory=list, repr=False)
    queue_bytes: int = field(default=0, repr=False)  # reserved in Limits for the queue
    queue_from: dict[str, int] = field(default_factory=dict, repr=False)  # sender id -> count
    seat_key: str | None = field(default=None, repr=False)  # IP key whose slot a waiting seat holds


@dataclass(eq=False, repr=False)
class Room:
    id: str
    peers: dict[str, Peer] = field(default_factory=dict)
    # Freed places, oldest first: a return gets resumed=False. At most LEFT_MAX; an id pushed
    # out is marked in `_evicted` and is still treated as freed (maybe also a few never-seen
    # ids, which then join as newcomers too). A new peer id is never one marked there.
    left: dict[str, None] = field(default_factory=dict)
    _evicted: bytearray | None = None
    _salt: bytes = field(default_factory=lambda: secrets.token_bytes(16))

    def __repr__(self) -> str:
        return f"Room(peers={len(self.peers)}, left={len(self.left)})"

    def free(self, peer_id: str) -> None:
        """Remember a freed place; the oldest one beyond LEFT_MAX goes to the bitmap."""
        self.left[peer_id] = None
        if len(self.left) > LEFT_MAX:
            oldest = next(iter(self.left))
            del self.left[oldest]
            if self._evicted is None:
                self._evicted = bytearray(_EVICTED_BITS // 8)
            bit = self._bit(oldest)
            self._evicted[bit // 8] |= 1 << (bit % 8)

    def freed(self, peer_id: str) -> bool:
        """The place was freed: the id is in `left` or (maybe) pushed out of it."""
        if peer_id in self.left:
            return True
        if self._evicted is None:
            return False
        bit = self._bit(peer_id)
        return bool(self._evicted[bit // 8] & (1 << (bit % 8)))

    def _bit(self, peer_id: str) -> int:
        digest = hashlib.blake2b(peer_id.encode(), digest_size=4, key=self._salt).digest()
        return int.from_bytes(digest) % _EVICTED_BITS


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
        # id(conn) -> (room id, peer id). An entry exists only while that peer's conn is
        # this very conn, so the conn stays referenced and its id cannot be reused.
        self._seats: dict[int, tuple[str, str]] = {}

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
        """The transport closed. A place in a room waits silently, keeping the conn's slot.

        Outside a room, or after a new join took the place over, the slot is released here.
        """
        located = self._locate(conn)
        self._seats.pop(id(conn), None)
        if located is None:
            self._limits.release_seat(conn.key)
            return
        _, peer = located
        peer.conn = None
        peer.disconnected_at = self._clock.now()
        peer.seat_key = conn.key

    def sweep(self) -> None:
        """Remove places that have waited WAIT_SECONDS or longer; empty rooms go with them."""
        for room in list(self._rooms.values()):
            self._expire(room)

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
        room_id, name, net = message.room_id, message.name, message.net
        room = self._rooms.get(room_id)
        if room is not None:
            # Places waiting WAIT_SECONDS or longer are free, as if the sweep had run.
            self._expire(room)
            room = self._rooms.get(room_id)
        peer_id = None
        if message.resume_token is not None:
            peer_id = verify_token(self._secret, room_id, message.resume_token)
        if peer_id is not None:
            if room is None:
                return self._new_room(conn, name, net, room_id=room_id, peer_id=peer_id)
            place = room.peers.get(peer_id)
            if place is not None:
                return self._resume(conn, room, place, net)
            if not room.freed(peer_id):  # the room was recreated after a restart
                if len(room.peers) >= ROOM_CAPACITY:
                    return self._error(conn, "room-full")
                return self._add_peer(conn, room, name, net, peer_id=peer_id)
            # The place was freed: join as a newcomer.
        if room is None:
            self._new_room(conn, name, net, room_id=room_id)
        elif len(room.peers) >= ROOM_CAPACITY:
            self._error(conn, "room-full")
        else:
            self._add_peer(conn, room, name, net)

    def _new_room(
        self,
        conn: Conn,
        name: str,
        net: NetInfo,
        *,
        room_id: str | None = None,
        peer_id: str | None = None,
    ) -> None:
        """Create a room. A valid token (peer_id) skips the per-key creation limit."""
        if len(self._rooms) >= self._limits.config.max_rooms or (
            peer_id is None and not self._limits.try_create(conn.key)
        ):
            return self._error(conn, "rate-limited")
        if room_id is None:
            room_id = new_room_id()
            while room_id in self._rooms:
                room_id = new_room_id()
        room = self._rooms[room_id] = Room(id=room_id)
        self._add_peer(conn, room, name, net, peer_id=peer_id)

    def _add_peer(
        self, conn: Conn, room: Room, name: str, net: NetInfo, *, peer_id: str | None = None
    ) -> None:
        """Add a peer: a newcomer gets a fresh id, a token holder keeps its id (resumed)."""
        resumed = peer_id is not None
        if peer_id is None:
            peer_id = self._new_peer_id(room)
            if peer_id is None:
                return self._error(conn, "room-full")
        peer = Peer(
            id=peer_id,
            conn=conn,
            name=name,
            muted=False,
            net=net,
            joined_at=self._clock.now(),
        )
        room.peers[peer_id] = peer
        self._seats[id(conn)] = (room.id, peer_id)
        self._broadcast(room, ServerPeerJoined(id=peer_id, name=name), skip=peer)
        self._send_joined(conn, room, peer, resumed=resumed)

    def _new_peer_id(self, room: Room) -> str | None:
        """An id that is not in the room and was never freed in it, or None."""
        for _ in range(_NEW_ID_TRIES):
            peer_id = new_peer_id()
            if peer_id not in room.peers and not room.freed(peer_id):
                return peer_id
        return None

    def _resume(self, conn: Conn, room: Room, peer: Peer, net: NetInfo) -> None:
        """Bind a new connection to an existing place. The others hear nothing."""
        old = peer.conn
        if old is not None:
            # The old connection releases its own slot when its close is reported.
            self._seats.pop(id(old), None)
            old.close(CLOSE_REPLACED)
        elif peer.seat_key is not None:
            self._limits.release_seat(peer.seat_key)  # the new connection has its own slot
            peer.seat_key = None
        peer.conn = conn
        peer.disconnected_at = None
        peer.net = net  # the name stays
        self._seats[id(conn)] = (room.id, peer.id)
        self._send_joined(conn, room, peer, resumed=True)
        for sender_id, text in peer.queue:
            # A sender who has left since is not in `peers` of this joined: drop its signals.
            if sender_id in room.peers:
                conn.send(text)
        self._drop_queue(peer)

    def _send_joined(self, conn: Conn, room: Room, peer: Peer, *, resumed: bool) -> None:
        others = [
            PeerInfo(id=p.id, name=p.name, muted=p.muted)
            for p in room.peers.values()
            if p is not peer
        ]
        conn.send(
            _text(
                ServerJoined(
                    room_id=room.id,
                    self_id=peer.id,
                    resumed=resumed,
                    resume_token=issue_token(self._secret, room.id, peer.id),
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

    def _expire(self, room: Room) -> None:
        """Remove the room's places that have waited WAIT_SECONDS or longer."""
        now = self._clock.now()
        for peer in list(room.peers.values()):
            if (
                peer.conn is None
                and peer.disconnected_at is not None
                and now - peer.disconnected_at >= WAIT_SECONDS
            ):
                self._remove_peer(room, peer)

    def _remove_peer(self, room: Room, peer: Peer) -> None:
        """Remove a peer for good: free the seat and its slot, drop its queue, tell the others."""
        del room.peers[peer.id]
        room.free(peer.id)
        self._drop_queue(peer)
        if peer.seat_key is not None:
            self._limits.release_seat(peer.seat_key)
            peer.seat_key = None
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
        text = dump_forwarded_signal(sender.id, message)
        if target.conn is not None:
            target.conn.send(text)
        else:
            self._enqueue(target, sender.id, text)

    def _enqueue(self, target: Peer, sender_id: str, text: str) -> None:
        """Queue a signal for a waiting peer. Over a limit the new one is dropped."""
        size = len(text.encode())
        if (
            len(target.queue) >= WAIT_QUEUE_MAX
            or target.queue_bytes + size > WAIT_QUEUE_BYTES
            or target.queue_from.get(sender_id, 0) >= WAIT_QUEUE_PER_SENDER
            or not self._limits.reserve(size)
        ):
            return
        target.queue.append((sender_id, text))
        target.queue_bytes += size
        target.queue_from[sender_id] = target.queue_from.get(sender_id, 0) + 1

    def _drop_queue(self, peer: Peer) -> None:
        self._limits.free(peer.queue_bytes)
        peer.queue.clear()
        peer.queue_bytes = 0
        peer.queue_from.clear()

    def _mute(self, conn: Conn, message: ClientMute) -> None:
        located = self._locate(conn)
        if located is None:
            return self._error(conn, "bad-request")
        room, peer = located
        peer.muted = message.muted
        self._broadcast(room, ServerMute(id=peer.id, muted=message.muted), skip=peer)
