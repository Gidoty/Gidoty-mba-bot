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
import time

from . import exchange
from .config import Settings
from .db import list_active_accounts, list_live_unsettled_trades, list_open_stop_limit_trades, session_scope
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


async def _recover_one(account_id: str, settings: Settings, *, current_price: float | None = None) -> None:
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
                await reconcile_account(session, account, settings, notifier, current_price=current_price)
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


async def monitor_stop_limits_once(settings: Settings) -> None:
    """Fast protection-health watch for Spot positions guarded by a
    STOP_LOSS_LIMIT, whose protection can silently stop working: if the
    price gaps through the limit the order rests unfilled.

    Traffic is bounded: with no such position nothing is called at all;
    otherwise ONE public ticker call per distinct symbol per pass, shared by
    every customer (no credentials, minimal weight). Only a position whose
    price has actually reached its trigger gets authenticated calls, and
    only until it is resolved. For those, trader.reconcile_account establishes
    the order's true state; lifecycle.monitor_trade then closes it if the
    price is through the executable limit (cancel -> confirm -> market-close
    the remainder), through the same exit lease as every other exit."""
    with session_scope() as session:
        watched = [(t.account_id, t.pair or session.get(TradingAccount, t.account_id).pair, t.stop_loss_price)
                   for t in list_open_stop_limit_trades(session)]
    if not watched:
        return

    symbols = sorted({exchange.exchange_symbol(pair, "spot") for _, pair, _ in watched})
    client = exchange.build_exchange_client(exchange="binance", market_type="spot", api_key="", api_secret="", sandbox=False)
    prices: dict[str, float] = {}
    try:
        for symbol in symbols:
            try:
                ticker = await client.fetch_ticker(symbol)
                last = ticker.get("last") or ticker.get("close")
                if last:
                    prices[symbol] = float(last)
            except Exception as exc:
                logger.warning("stop-limit watch: ticker for %s unavailable (%s)", symbol, type(exc).__name__)
    finally:
        await client.close()

    due = []
    for account_id, pair, trigger in watched:
        price = prices.get(exchange.exchange_symbol(pair, "spot"))
        if price is not None and price <= trigger:  # Spot is long-only: the stop is below the market
            due.append((account_id, price))
    if due:
        logger.info("stop-limit watch: %d position(s) at or through their trigger", len(due))
        await asyncio.gather(*(_recover_one(a, settings, current_price=p) for a, p in due), return_exceptions=True)


async def run_recovery_forever(settings: Settings) -> None:
    """Fast loop (RECOVERY_INTERVAL_SECONDS, default 15s) so an unprotected
    or unresolved live trade is not left waiting for the 5-minute poll. The
    stop-limit price watch rides the same tick, at most once per
    STOP_LIMIT_MONITOR_INTERVAL_SECONDS."""
    last_watch = float("-inf")
    while True:
        await asyncio.sleep(settings.recovery_interval_seconds)
        try:
            await recover_once(settings)
        except Exception:
            logger.exception("unexpected error in recovery loop")
        if time.monotonic() - last_watch >= settings.stop_limit_monitor_interval_seconds:
            last_watch = time.monotonic()
            try:
                await monitor_stop_limits_once(settings)
            except Exception:
                logger.exception("unexpected error in stop-limit watch")


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
