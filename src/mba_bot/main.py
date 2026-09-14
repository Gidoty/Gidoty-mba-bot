"""Entrypoint: `python -m mba_bot.main` runs the shared scheduler loop."""
from __future__ import annotations

import asyncio
import logging

from .config import load_settings
from .db import init_db
from .scheduler import run_forever


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    configure_logging()
    settings = load_settings()
    if not settings.encryption_key:
        raise SystemExit("ENCRYPTION_KEY is not set - generate one with `python -m mba_bot.crypto_utils`")
    init_db(settings.database_url)
    asyncio.run(run_forever(settings))


if __name__ == "__main__":
    main()
