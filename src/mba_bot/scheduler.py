"""The shared event loop: one process, iterating every active customer
account each poll interval, concurrently via asyncio.

See ARCHITECTURE.md for why this is one shared loop rather than one OS
process per customer. The key property this module has to preserve is
isolation without processes: `_run_one` never lets an exception from one
account's cycle propagate into another's, and a fresh DB session/notifier
per poll keeps accounts from sharing mutable state across a cycle.
"""
from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import list_active_accounts, session_scope
from .models import TradingAccount
from .notifications import build_notifier
from .trader import process_account

logger = logging.getLogger(__name__)


async def _run_one(account_id: str, settings: Settings) -> None:
    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        if account is None or not account.is_active:
            return
        try:
            notifier = build_notifier(
                account.notification_channel,
                telegram_bot_token=settings.telegram_bot_token,
                whatsapp_provider_token=settings.whatsapp_provider_token,
            )
        except Exception:
            logger.exception("account %s: could not build notifier for channel=%s", account.id, account.notification_channel)
            return

        try:
            await process_account(session, account, settings, notifier)
        except Exception:
            logger.exception("account %s: error during processing cycle", account.id)


async def run_once(settings: Settings) -> None:
    with session_scope() as session:
        account_ids = [a.id for a in list_active_accounts(session)]

    if not account_ids:
        logger.info("no active accounts to process")
        return

    logger.info("processing %d active account(s)", len(account_ids))
    await asyncio.gather(*(_run_one(account_id, settings) for account_id in account_ids), return_exceptions=True)


async def run_forever(settings: Settings) -> None:
    while True:
        try:
            await run_once(settings)
        except Exception:
            logger.exception("unexpected error in scheduler loop")
        await asyncio.sleep(settings.poll_interval_seconds)
