"""FastAPI app: the public API, plus the trading scheduler running as a
background task in the same process.

Deliberately one process: the scheduler loop (`scheduler.run_forever`)
is started in the lifespan hook below rather than as a separate service.
This keeps the "one process, one server" deployment shape from
ARCHITECTURE.md even though that process now also answers HTTP - see
that doc for why a shared loop beats one process per customer, which
applies just as much to "one extra process for the API" as it does to
per-customer processes.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import load_settings, warn_about_legacy_futures_testnet_env
from ..db import init_db
from ..instance_lock import AlreadyRunning, acquire_single_instance_lock
from ..log_safety import install_log_safety
from ..scheduler import run_forever
from .ratelimit import RateLimitMiddleware
from .routers import account, auth, billing, pairs, telegram, trades

logger = logging.getLogger(__name__)


def _ensure_scheduler_logs_are_visible() -> None:
    """Under `uvicorn mba_bot.api.app:app` nothing configures the root logger,
    so Python's last-resort handler prints WARNING and above only, with no
    level or timestamp - the scheduler's INFO heartbeat ("round complete ...")
    would be invisible and an empty journal would prove nothing. Give the
    package its own stream handler (INFO) unless logging is already set up
    (main.py's basicConfig, pytest, a host application). Redaction does not
    depend on handlers: it happens when each record is created."""
    package_logger = logging.getLogger("mba_bot")
    if package_logger.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.INFO)


def _log_scheduler_task_ended(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    logger.critical("the scheduler task ended unexpectedly%s", "" if exc is None else f": {type(exc).__name__}", exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is not set - generate one with `python -m mba_bot.crypto_utils`")
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not set")

    warn_about_legacy_futures_testnet_env(logger)
    install_log_safety(settings)  # idempotent; also covers `uvicorn mba_bot.api.app:app`
    lock_handle = None
    if settings.api_single_instance_lock:
        # F-03: production starts through uvicorn, which never runs main.py, so the single-instance guard has to be
        # taken here. A second worker or a second copy of the service refuses to start instead of running a second scheduler.
        try:
            lock_handle = acquire_single_instance_lock(settings.lock_file)
        except AlreadyRunning as exc:
            raise RuntimeError(str(exc)) from None
    init_db(settings.database_url, wal=settings.sqlite_wal, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    _ensure_scheduler_logs_are_visible()
    scheduler_task = asyncio.create_task(run_forever(settings))
    scheduler_task.add_done_callback(_log_scheduler_task_ended)
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        if lock_handle is not None:
            lock_handle.close()  # releases the flock


def create_app() -> FastAPI:
    settings = load_settings()
    # F-06: with DISABLE_API_DOCS on, the interactive docs and the schema are not served at all.
    docs_off = {"docs_url": None, "redoc_url": None, "openapi_url": None} if settings.disable_api_docs else {}
    app = FastAPI(title="MBA Bot API", lifespan=lifespan, **docs_off)

    if settings.rate_limit_enabled:  # F-04, second line behind Nginx. Added BEFORE the CORS layer so it sits inside it:
        # a 429 still carries the CORS headers and the browser shows the real status instead of a CORS error.
        app.add_middleware(RateLimitMiddleware, per_minute=settings.rate_limit_per_minute, auth_per_minute=settings.rate_limit_auth_per_minute)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(trades.router)
    app.include_router(telegram.router)
    app.include_router(billing.router)
    app.include_router(pairs.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
