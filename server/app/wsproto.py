"""uvicorn's WebSocket protocol with limits on what the client makes the server do (H1).

Run uvicorn with `--ws app.wsproto:TeteWebSocketProtocol`. On top of the websockets sans-I/O
protocol of uvicorn, this class:

- counts the raw bytes and frames of every read before they are parsed (app/frames.py) and
  aborts the transport over budget: uvicorn answers PING itself and joins fragments itself, so
  the application's per-message limits never see them. A message in more than
  MESSAGE_FRAMES_MAX frames is aborted the same way: uvicorn would hold every fragment;
- writes no PONG while the transport's write buffer is above PONG_BUFFER_LIMIT: a client that
  pings but does not read cannot grow the buffer with replies;
- aborts the transport when the keepalive ping gets no answer: uvicorn calls close(), which
  waits for a full write buffer forever, and the application never hears of the disconnect;
- hands the application the transport's abort through the ASGI scope (ABORT_EXTENSION), so
  that a connection the server closed is torn down even if the client never reads.

It logs nothing of its own. The base class logs the client address only at TRACE level, which
the privacy filter in app/logs.py drops. The class overrides uvicorn internals: uvicorn is
pinned to UVICORN_VERSION and tests/test_wsproto.py checks those internals.
"""

import asyncio
from typing import Any, ClassVar, Final

from uvicorn.config import Config
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from uvicorn.server import ServerState
from websockets.http11 import Request

from app.clock import Clock, MonotonicClock
from app.frames import RawBudget
from app.ws import ABORT_EXTENSION

UVICORN_VERSION: Final = "0.54.0"
# The transport's default high-water mark: below it the application's own writes are not
# held back either, so a client that reads gets its PONGs.
PONG_BUFFER_LIMIT: Final = 65_536
# A server frame with FIN set and the PONG opcode, as websockets serializes it.
_PONG_HEAD: Final = 0x8A


class TeteWebSocketProtocol(WebSocketsSansIOProtocol):
    clock: ClassVar[Clock] = MonotonicClock()

    def __init__(
        self,
        config: Config,
        server_state: ServerState,
        app_state: dict[str, Any],
        _loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(config, server_state, app_state, _loop)
        self._budget = RawBudget(self.clock.now())
        self._dropped = False

    def data_received(self, data: bytes) -> None:
        if self._dropped:
            return
        if not self._budget.admit(data, self.clock.now()):
            self._drop()
            return
        super().data_received(data)

    def handle_connect(self, event: Request) -> None:
        super().handle_connect(event)
        if self.handshake_initiated and not self.handshake_complete:
            # The handshake goes to the application: the task that runs it has not started.
            self.scope.setdefault("extensions", {})[ABORT_EXTENSION] = {"abort": self._drop}

    def handle_ping(self) -> None:
        """Send what the PINGs produced, without the PONGs if the client is not reading."""
        output = self.conn.data_to_send()
        if self.transport.get_write_buffer_size() > PONG_BUFFER_LIMIT:
            output = [chunk for chunk in output if chunk[:1] != bytes([_PONG_HEAD])]
        if output:
            self.transport.write(b"".join(output))

    def keepalive_timeout(self) -> None:
        """No PONG in time: drop the connection at once instead of a graceful close."""
        self.pong_timer = None
        self.pending_ping_payload = None
        self._drop()

    def _drop(self) -> None:
        """Abort the transport: connection_lost follows and the application gets a
        disconnect, whatever is still in the write buffer."""
        self._dropped = True
        self.transport.abort()
