import asyncio
import logging
import sys
from collections.abc import Iterator

import pytest

from app import logs

NAMES = ("uvicorn", "uvicorn.error", "websockets")


@pytest.fixture(autouse=True)
def installed() -> Iterator[None]:
    logs.install_log_filter()
    yield
    for name in NAMES:
        lg = logging.getLogger(name)
        for f in [f for f in lg.filters if isinstance(f, logs.PrivacyFilter)]:
            lg.removeFilter(f)


@pytest.mark.parametrize(
    "args", [("203.0.113.7:5555", "/ws"), ("203.0.113.7:5555", "/ws?r=abcdefghijkm")]
)
@pytest.mark.parametrize(
    "fmt", ['%s - "WebSocket %s" [accepted]', '%s - "WebSocket %s" 403', '%s - "WebSocket %s" %d']
)
def test_handshake_records_dropped(
    caplog: pytest.LogCaptureFixture, fmt: str, args: tuple[str, str]
) -> None:
    caplog.set_level(logging.DEBUG)
    full = (*args, 403) if fmt.endswith("%d") else args
    logging.getLogger("uvicorn.error").info(fmt, *full)
    assert caplog.records == []
    assert "203.0.113.7" not in caplog.text


def test_debug_and_trace_from_uvicorn_and_websockets_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(1)
    for name in ("uvicorn.error", "uvicorn", "websockets"):
        lg = logging.getLogger(name)
        lg.debug("frame %s", "203.0.113.7")
        lg.log(5, "%sWebSocket connection made", "203.0.113.7:1 - ")
    assert caplog.records == []


def test_info_startup_message_kept(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 42)
    assert "Started server process [42]" in caplog.text


def test_install_is_idempotent() -> None:
    logs.install_log_filter()
    logs.install_log_filter()
    for name in NAMES:
        found = [f for f in logging.getLogger(name).filters if isinstance(f, logs.PrivacyFilter)]
        assert len(found) == 1


LEAKY = "Аня abcdefghijkm tok.en 203.0.113.7"


def _check_clean(text: str) -> None:
    assert "ValueError" in text
    for s in ("Аня", "abcdefghijkm", "tok.en", "203.0.113.7", "Traceback"):
        assert s not in text


def test_log_exception_writes_type_name_only(caplog: pytest.LogCaptureFixture) -> None:
    try:
        raise ValueError(LEAKY)
    except ValueError as exc:
        logs.log_exception(logs.logger, "ws handler", exc)
    _check_clean(caplog.text)
    assert "ws handler failed: ValueError" in caplog.text


def test_loop_exception_handler_writes_type_name_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = asyncio.new_event_loop()
    try:
        logs.loop_exception_handler(
            loop, {"message": f"Task {LEAKY}", "exception": ValueError(LEAKY)}
        )
        _check_clean(caplog.text)
        caplog.clear()
        logs.loop_exception_handler(loop, {"message": LEAKY})
        assert "203.0.113.7" not in caplog.text
    finally:
        loop.close()


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def capture() -> Iterator[_Capture]:
    handler = _Capture()
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logs.install_log_filter()
    logs.install_log_filter()
    yield handler
    root.removeHandler(handler)
    root.setLevel(old)


def test_child_loggers_filtered_via_handler(capture: _Capture) -> None:
    logging.getLogger("websockets.server").debug("frame %s", "203.0.113.7")
    logging.getLogger("uvicorn.error.child").info(
        '%s - "WebSocket %s" [accepted]', "203.0.113.7:1", "/ws"
    )
    logging.getLogger("tete").info("kept")
    assert capture.lines == ["tete kept"]
    assert len(capture.filters) == 1


def test_traceback_replaced_by_type_name(capture: _Capture) -> None:
    try:
        raise ValueError(LEAKY)
    except ValueError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application\n")
        logging.getLogger("websockets.server").error("opening handshake failed", exc_info=True)
    text = "\n".join(capture.lines)
    _check_clean(text)
    assert len(capture.lines) == 2


def test_last_resort_handler_filtered(capsys: pytest.CaptureFixture[str]) -> None:
    logs.install_log_filter()
    logs.install_log_filter()
    last = logging.lastResort
    assert last is not None
    assert len([f for f in last.filters if isinstance(f, logs.PrivacyFilter)]) == 1
    try:
        raise ValueError(LEAKY)
    except ValueError:
        failed = logging.LogRecord(
            "websockets.server", logging.ERROR, __file__, 1, "connection failed", (), sys.exc_info()
        )
    handshake = logging.LogRecord(
        "uvicorn.error",
        logging.WARNING,
        __file__,
        1,
        '%s - "WebSocket %s" 403',
        ("203.0.113.7:1", "/ws"),
        None,
    )
    last.handle(failed)
    last.handle(handshake)
    err = capsys.readouterr().err
    _check_clean(err)
    assert "connection failed (ValueError)" in err


@pytest.mark.parametrize(
    ("last_line", "name"),
    [
        (f"app.config.ConfigError: {LEAKY}", "ConfigError"),
        (f"ValueError: {LEAKY}", "ValueError"),
        ("KeyboardInterrupt", "KeyboardInterrupt"),
        (f"{LEAKY}", "unknown"),
    ],
)
def test_traceback_text_in_message_replaced_by_type_name(
    capture: _Capture, last_line: str, name: str
) -> None:
    # Starlette sends the lifespan failure as formatted text; uvicorn logs it as the message.
    text = (
        "Traceback (most recent call last):\n"
        f'  File "x.py", line 1, in f\n    g("{LEAKY}")\n'
        f"{last_line}\n"
    )
    logging.getLogger("uvicorn.error").error(text)
    logging.getLogger("uvicorn.error").error("%s", text)
    assert capture.lines == [f"uvicorn.error traceback omitted ({name})"] * 2
