"""Application factory for integration tests (`uvicorn --factory`). Not used in production.

TETE_TEST_LIMITS: JSON with LimitConfig fields to override.
TETE_TEST_FAULT=1: a join with the name "boom" raises an exception full of personal data,
to check that only its type name reaches the log.
"""

import json
import os
from typing import Final

from fastapi import FastAPI

from app.limits import LimitConfig
from app.main import create_app
from app.protocol import ClientJoin, ClientMessage
from app.rooms import Conn, Hub

FAULT_NAME: Final = "boom"


class FaultyHub(Hub):
    def handle(self, conn: Conn, message: ClientMessage) -> None:
        if isinstance(message, ClientJoin) and message.name == FAULT_NAME:
            raise ValueError(f"{message.name} {message.room_id} {message.resume_token}")
        super().handle(conn, message)


def make_app() -> FastAPI:
    raw = os.environ.get("TETE_TEST_LIMITS")
    limit_config = LimitConfig(**json.loads(raw)) if raw else None
    hub_factory = FaultyHub if os.environ.get("TETE_TEST_FAULT") == "1" else Hub
    return create_app(limit_config=limit_config, hub_factory=hub_factory)
