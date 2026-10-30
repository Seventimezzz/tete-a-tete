"""Точка входа сигналинга Tete-a-tete. HTTP-API нет, Caddy проксирует только /ws."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from typing import Final

from fastapi import FastAPI

from app.clock import Clock, MonotonicClock
from app.config import ConfigError, Settings, load_settings
from app.limits import LimitConfig, Limits
from app.logs import install_log_filter, log_exception, logger, loop_exception_handler
from app.rooms import Hub
from app.ws import WsState, router

SWEEP_SECONDS: Final = 1.0
STATS_SECONDS: Final = 60.0


def _install_app_log_handler() -> None:
    """uvicorn configures only its own loggers: give ours a stderr handler, or the stats
    line (INFO) would be dropped. Idempotent."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


async def _sweep_forever(state: WsState) -> None:
    """Once a second: expire waiting places and limit windows; every 60 s a stats line."""
    last_stats = state.clock.now()
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            state.hub.sweep()
            state.limits.sweep()
            now = state.clock.now()
            if now - last_stats >= STATS_SECONDS:
                last_stats = now
                stats = state.hub.stats()
                logger.info(
                    "stats rooms=%d peers=%d waiting=%d connections=%d closed_by_limits=%d",
                    stats.rooms,
                    stats.peers,
                    stats.waiting,
                    state.limits.connections,
                    state.closed_by_limits,
                )
        except Exception as exc:
            log_exception(logger, "sweeper", exc)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    limit_config: LimitConfig | None = None,
    hub_factory: Callable[..., Hub] = Hub,
    close_wait: float | None = None,
) -> FastAPI:
    """Settings left out are read from the environment at startup, not at import.

    `close_wait` replaces the close window of app.ws.CLOSE_WAIT_SECONDS; for tests only.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            current = settings if settings is not None else load_settings(os.environ)
        except ConfigError as exc:
            logger.error("startup failed: %s", exc)  # the message never holds the secret
            raise
        used_clock = clock if clock is not None else MonotonicClock()
        limits = Limits(limit_config if limit_config is not None else LimitConfig(), used_clock)
        hub = hub_factory(secret=current.resume_secret, clock=used_clock, limits=limits)
        state = WsState(hub=hub, limits=limits, clock=used_clock, close_wait=close_wait)
        app.state.ws = state
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(loop_exception_handler)
        sweeper = asyncio.create_task(_sweep_forever(state))
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            loop.set_exception_handler(previous_handler)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.include_router(router)
    return app


install_log_filter()
_install_app_log_handler()
app = create_app()
