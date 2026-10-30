"""Settings loaded from the environment."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

RESUME_SECRET_MIN_LENGTH: Final = 32


class ConfigError(Exception):
    """Invalid configuration; the message never contains secret values."""


@dataclass(frozen=True)
class Settings:
    resume_secret: bytes = field(repr=False)


def load_settings(env: Mapping[str, str]) -> Settings:
    secret = env.get("RESUME_SECRET")
    if not secret:
        raise ConfigError("RESUME_SECRET is missing")
    if len(secret) < RESUME_SECRET_MIN_LENGTH:
        raise ConfigError(f"RESUME_SECRET must be at least {RESUME_SECRET_MIN_LENGTH} characters")
    return Settings(resume_secret=secret.encode())
