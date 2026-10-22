"""Logging without personal data: handshake filter and exception writers."""

import asyncio
import logging
from typing import Any, Final

logger: Final = logging.getLogger("tete")

_HANDSHAKE_PREFIX: Final = '%s - "WebSocket %s"'
_FILTERED_LOGGERS: Final = ("uvicorn", "uvicorn.error", "websockets")


class PrivacyFilter(logging.Filter):
    """Drops handshake records (they carry the client IP) and sub-INFO noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and record.msg.startswith(_HANDSHAKE_PREFIX):
            return False
        return not (
            record.levelno < logging.INFO and record.name.startswith(("uvicorn", "websockets"))
        )


def install_log_filter() -> None:
    """Attach the filter to uvicorn and websockets loggers, once."""
    for name in _FILTERED_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, PrivacyFilter) for f in target.filters):
            target.addFilter(PrivacyFilter())


def log_exception(log: logging.Logger, where: str, exc: BaseException) -> None:
    """Log only the exception type name: messages may hold IPs, SDP or ids."""
    log.error("%s failed: %s", where, type(exc).__name__)


def loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Event loop handler: type name only, never context["message"]."""
    exc = context.get("exception")
    name = type(exc).__name__ if isinstance(exc, BaseException) else "unknown"
    logger.error("event loop failed: %s", name)
