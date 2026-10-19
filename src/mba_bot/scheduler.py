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
from .db import list_active_accounts, list_live_unsettled_trades, session_scope
from .models import PROTECTION_PROTECTED, TRADE_OPEN, TradingAccount
from .notifications import build_notifier
from .trader import process_account, reconcile_account

logger = logging.getLogger(__name__)

# One lock per account, shared by the main poll loop and the fast recovery
# loop, so the two never work the same account concurrently inside this
# process (the database claims and deterministic order ids cover the rest).
_account_locks: dict[str, asyncio.Lock] = {}


def _lock_for(account_id: str) -> asyncio.Lock:
    lock = _account_locks.get(account_id)
    if lock is None:
        lock = _account_locks[account_id] = asyncio.Lock()
    return lock


async def _run_one(account_id: str, settings: Settings) -> None:
    async with _lock_for(account_id):
        await _run_one_locked(account_id, settings)


async def _run_one_locked(account_id: str, settings: Settings) -> None:
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


async def _recover_one(account_id: str, settings: Settings) -> None:
    async with _lock_for(account_id):
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            if account is None:
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
                await reconcile_account(session, account, settings, notifier)
            except Exception:
                logger.exception("account %s: error during reconciliation", account.id)


async def recover_once(settings: Settings, *, include_healthy: bool = False) -> None:
    """Reconcile live trades that are not in a healthy, verified-protected
    state (pending entry, protecting, closing, attention, or open with no
    verified stop). With `include_healthy`, every non-terminal live trade -
    used once at startup so nothing is trusted from SQLite alone."""
    with session_scope() as session:
        account_ids = sorted(
            {
                t.account_id
                for t in list_live_unsettled_trades(session)
                if include_healthy or t.status != TRADE_OPEN or t.protection_status != PROTECTION_PROTECTED
            }
        )
    if account_ids:
        logger.info("reconciling %d account(s) with live trades needing attention", len(account_ids))
        await asyncio.gather(*(_recover_one(a, settings) for a in account_ids), return_exceptions=True)


async def run_recovery_forever(settings: Settings) -> None:
    """Fast loop (RECOVERY_INTERVAL_SECONDS, default 15s) so an unprotected
    or unresolved live trade is not left waiting for the 5-minute poll."""
    while True:
        await asyncio.sleep(settings.recovery_interval_seconds)
        try:
            await recover_once(settings)
        except Exception:
            logger.exception("unexpected error in recovery loop")


async def run_poll_forever(settings: Settings) -> None:
    while True:
        try:
            await run_once(settings)
        except Exception:
            logger.exception("unexpected error in scheduler loop")
        await asyncio.sleep(settings.poll_interval_seconds)


async def run_forever(settings: Settings) -> None:
    # Startup reconciliation: before the first poll, bring every live trade
    # in line with Binance. SQLite alone is never trusted after a restart.
    try:
        await recover_once(settings, include_healthy=True)
    except Exception:
        logger.exception("startup reconciliation failed")
    await asyncio.gather(run_poll_forever(settings), run_recovery_forever(settings))
