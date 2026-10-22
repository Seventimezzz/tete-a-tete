"""Logging without personal data: handshake filter and exception writers."""

import asyncio
import logging
import re
from typing import Any, Final

logger: Final = logging.getLogger("tete")

_HANDSHAKE_PREFIX: Final = '%s - "WebSocket %s"'
_FAMILIES: Final = ("uvicorn", "websockets")
_FILTERED_LOGGERS: Final = ("uvicorn", "uvicorn.error", "websockets")
_TRACEBACK: Final = "Traceback (most recent call last):"
_EXC_NAME: Final = re.compile(r"[A-Z][A-Za-z0-9_]*")


def _in_family(name: str) -> bool:
    return any(name == root or name.startswith(root + ".") for root in _FAMILIES)


def _exception_name(traceback_text: str) -> str:
    """Type name from the last line of a formatted traceback ("pkg.Type: message")."""
    lines = [line for line in traceback_text.splitlines() if line.strip()]
    name = lines[-1].split(":", 1)[0].strip().rsplit(".", 1)[-1] if lines else ""
    return name if _EXC_NAME.fullmatch(name) else "unknown"


class PrivacyFilter(logging.Filter):
    """Drops handshake records (they carry the client IP) and sub-INFO noise.

    For uvicorn and websockets records with a traceback (as exc_info or as text in the
    message), keeps only the exception type name.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not _in_family(record.name):
            return True
        if isinstance(record.msg, str) and record.msg.startswith(_HANDSHAKE_PREFIX):
            return False
        if record.levelno < logging.INFO:
            return False
        try:
            message = record.getMessage()
        except Exception:
            message = ""
        if _TRACEBACK in message:
            # Starlette passes a lifespan failure to uvicorn as formatted text, not exc_info.
            record.msg = f"traceback omitted ({_exception_name(message)})"
            record.args = None
        if record.exc_info is not None and record.exc_info[0] is not None:
            record.msg = f"{record.getMessage().rstrip()} ({record.exc_info[0].__name__})"
            record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def install_log_filter() -> None:
    """Attach the filter to uvicorn/websockets loggers and to the handlers that
    emit their records (a logger filter does not see records from child loggers),
    including logging.lastResort, used while the root logger has no handlers.
    Idempotent."""
    loggers = [logging.getLogger(n) for n in _FILTERED_LOGGERS]
    targets: list[logging.Filterer] = list(loggers)
    for owner in (logging.getLogger(), *loggers[:2]):
        targets.extend(owner.handlers)
    if logging.lastResort is not None:
        targets.append(logging.lastResort)
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
