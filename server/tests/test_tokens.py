import re

import pytest

from app.tokens import issue_token, verify_token

SHAPE_RE = r"^[a-km-np-z2-9]{8}\.[A-Za-z0-9_-]{43}$"
SECRET = b"k" * 32
ROOM = "abcdefghijkm"
PEER = "pqrstuvw"


def test_issued_token_format_and_length() -> None:
    t = issue_token(SECRET, ROOM, PEER)
    assert re.fullmatch(SHAPE_RE, t)
    assert len(t) == 52


def test_token_is_deterministic() -> None:
    assert issue_token(SECRET, ROOM, PEER) == issue_token(SECRET, ROOM, PEER)


def test_verify_returns_peer_id() -> None:
    assert verify_token(SECRET, ROOM, issue_token(SECRET, ROOM, PEER)) == PEER


def _flip(token: str) -> str:
    last = token[-1]
    return token[:-1] + ("A" if last != "A" else "B")


@pytest.mark.parametrize(
    "case",
    [
        "other room",
        "other secret",
        "flipped signature char",
        "no dot",
        "peer id outside alphabet",
        "128 chars garbage",
        "empty",
        "signature with padding",
    ],
)
def test_bad_token_is_none(case: str) -> None:
    good = issue_token(SECRET, ROOM, PEER)
    secret, room = SECRET, ROOM
    token = {
        "other room": good,
        "other secret": good,
        "flipped signature char": _flip(good),
        "no dot": good.replace(".", ""),
        "peer id outside alphabet": "1loO0000" + good[8:],
        "128 chars garbage": "x" * 128,
        "empty": "",
        "signature with padding": good + "=",
    }[case]
    if case == "other room":
        room = "mnpqrstuvwxy"
    if case == "other secret":
        secret = b"z" * 32
    assert verify_token(secret, room, token) is None
