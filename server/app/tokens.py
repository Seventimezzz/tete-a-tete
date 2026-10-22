"""Stateless resume tokens: `<peerId>.<base64url HMAC-SHA256>`."""

import base64
import hashlib
import hmac

from app.protocol import ID_ALPHABET, PEER_ID_LENGTH

_SIGNATURE_LENGTH = 43


def _sign(secret: bytes, room_id: str, peer_id: str) -> str:
    digest = hmac.new(secret, f"v1\n{room_id}\n{peer_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue_token(secret: bytes, room_id: str, peer_id: str) -> str:
    return f"{peer_id}.{_sign(secret, room_id, peer_id)}"


def verify_token(secret: bytes, room_id: str, token: str) -> str | None:
    """Return the peer id carried by a valid token, otherwise None."""
    peer_id, dot, signature = token.partition(".")
    if not dot or len(peer_id) != PEER_ID_LENGTH or len(signature) != _SIGNATURE_LENGTH:
        return None
    if any(ch not in ID_ALPHABET for ch in peer_id):
        return None
    expected = _sign(secret, room_id, peer_id)
    if hmac.compare_digest(signature.encode(), expected.encode()):
        return peer_id
    return None
