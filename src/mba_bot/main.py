"""Entrypoint: `python -m mba_bot.main` runs the shared scheduler loop."""
from __future__ import annotations

import asyncio
import logging

from .config import load_settings
from .db import init_db
from .log_safety import install_log_safety
from .instance_lock import AlreadyRunning, acquire_single_instance_lock
from .scheduler import run_forever


def configure_logging(settings=None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Credential-bearing third-party HTTP loggers (httpx, httpcore, ccxt) pinned
    # to WARNING and every log record redacted at creation - so the root logger
    # can be set to DEBUG for troubleshooting without leaking secrets.
    install_log_safety(settings)


def main() -> None:
    settings = load_settings()
    configure_logging(settings)
    if not settings.encryption_key:
        raise SystemExit("ENCRYPTION_KEY is not set - generate one with `python -m mba_bot.crypto_utils`")
    try:
        # Held (referenced) for the whole life of the process.
        lock = acquire_single_instance_lock(settings.lock_file)  # noqa: F841
    except AlreadyRunning as exc:
        raise SystemExit(str(exc))
    init_db(settings.database_url)
    asyncio.run(run_forever(settings))


if __name__ == "__main__":
    main()
