"""QA application factory (`uvicorn --factory`): the production app with a clock the test moves.

The clock is the real monotonic clock plus an offset. A WebSocket on `/qa/clock` takes a number
of seconds as text, adds it to the offset and answers with the new offset, so the 15 s wait and
the 60 s windows are crossed without waiting. Not used in production.

TETE_TEST_LIMITS: JSON with LimitConfig fields to override, as in factory.py.
"""

import json
import os

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.clock import MonotonicClock
from app.limits import LimitConfig
from app.main import create_app


class OffsetClock:
    def __init__(self) -> None:
        self._base = MonotonicClock()
        self.offset = 0.0

    def now(self) -> float:
        return self._base.now() + self.offset


def make_app() -> FastAPI:
    raw = os.environ.get("TETE_TEST_LIMITS")
    limit_config = LimitConfig(**json.loads(raw)) if raw else None
    clock = OffsetClock()
    app = create_app(clock=clock, limit_config=limit_config)

    @app.websocket("/qa/clock")
    async def advance(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                clock.offset += float(await websocket.receive_text())
                await websocket.send_text(str(clock.offset))
        except WebSocketDisconnect:
            return

    return app
