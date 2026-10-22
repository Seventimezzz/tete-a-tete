"""Logging without personal data: handshake filter and exception writers."""

import asyncio
import logging
from typing import Any, Final

logger: Final = logging.getLogger("tete")

_HANDSHAKE_PREFIX: Final = '%s - "WebSocket %s"'
_FAMILIES: Final = ("uvicorn", "websockets")
_FILTERED_LOGGERS: Final = ("uvicorn", "uvicorn.error", "websockets")


def _in_family(name: str) -> bool:
    return any(name == root or name.startswith(root + ".") for root in _FAMILIES)


class PrivacyFilter(logging.Filter):
    """Drops handshake records (they carry the client IP) and sub-INFO noise.

    For uvicorn and websockets records with a traceback, keeps only the exception type name.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not _in_family(record.name):
            return True
        if isinstance(record.msg, str) and record.msg.startswith(_HANDSHAKE_PREFIX):
            return False
        if record.levelno < logging.INFO:
            return False
        if record.exc_info is not None and record.exc_info[0] is not None:
            record.msg = f"{record.getMessage().rstrip()} ({record.exc_info[0].__name__})"
            record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def install_log_filter() -> None:
    """Attach the filter to uvicorn/websockets loggers and to the handlers that
    emit their records (a logger filter does not see records from child loggers).
    Idempotent."""
    loggers = [logging.getLogger(n) for n in _FILTERED_LOGGERS]
    targets: list[logging.Filterer] = list(loggers)
    for owner in (logging.getLogger(), *loggers[:2]):
        targets.extend(owner.handlers)
    for target in targets:
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
