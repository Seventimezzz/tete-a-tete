import traceback

import pytest

from app.clock import MonotonicClock
from app.config import RESUME_SECRET_MIN_LENGTH, ConfigError, Settings, load_settings
from tests.fakes import FakeClock


def test_fake_clock_advances() -> None:
    clock = FakeClock()
    assert clock.now() == 1000.0
    clock.advance(2.5)
    assert clock.now() == 1002.5


def test_monotonic_clock_does_not_go_back() -> None:
    clock = MonotonicClock()
    values = [clock.now() for _ in range(100)]
    assert values == sorted(values)


def test_missing_secret_is_config_error() -> None:
    with pytest.raises(ConfigError, match="RESUME_SECRET is missing"):
        load_settings({})


def test_short_secret_error_does_not_contain_it() -> None:
    secret = "Zq7-" + "s" * 27
    assert len(secret) == RESUME_SECRET_MIN_LENGTH - 1
    with pytest.raises(ConfigError) as info:
        load_settings({"RESUME_SECRET": secret})
    exc = info.value
    assert "Zq7-" not in str(exc)
    assert "Zq7-" not in repr(exc)
    assert "Zq7-" not in "".join(traceback.format_exception(exc))
    assert "at least 32" in str(exc)


def test_secret_of_32_chars_is_accepted() -> None:
    secret = "Zq7-" + "s" * 28
    assert load_settings({"RESUME_SECRET": secret}).resume_secret == secret.encode()


def test_settings_repr_hides_secret() -> None:
    secret = "Zq7-" + "s" * 28
    assert "Zq7-" not in repr(Settings(resume_secret=secret.encode()))
