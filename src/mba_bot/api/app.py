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

from ..config import load_settings
from ..db import init_db
from ..scheduler import run_forever
from .routers import account, auth, billing, telegram, trades

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is not set - generate one with `python -m mba_bot.crypto_utils`")
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not set")

    init_db(settings.database_url)
    scheduler_task = asyncio.create_task(run_forever(settings))
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="MBA Bot API", lifespan=lifespan)

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

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
