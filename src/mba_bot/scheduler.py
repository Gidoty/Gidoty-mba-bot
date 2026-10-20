"""The shared event loop: one process, iterating every active customer
account each poll interval, concurrently via asyncio.

See ARCHITECTURE.md for why this is one shared loop rather than one OS
process per customer. The properties this module has to preserve:

* ISOLATION without processes. One account's cycle can fail, stall or be slow
  without stopping another's. `_guarded_cycle` never lets an exception escape,
  and `run_once` neither waits on a straggler nor lets one pile up.
* NO WRITE LOCK ACROSS A SUSPENSION. Cycles overlap on the event loop, each
  with its own SQLite connection. SQLite has one database-wide write lock, and
  sqlite3 waits for it SYNCHRONOUSLY, freezing the whole loop. If a cycle held
  that lock across an `await` (an exchange call, Telegram) a second cycle's
  write would block the loop - so the first could never resume to release it -
  and fail after the busy timeout. That is how a newer account used to be
  starved forever, silently, by an older one. Scheduler sessions therefore
  never flush implicitly (`session_scope(autoflush=False)`): they write only at
  explicit commits, each a short synchronous stretch, so cycles cannot contend.
* NOTHING FAILS SILENTLY. Every failure is logged with the account id we
  already hold (never read from an ORM object: after a failed flush that very
  read raises and used to swallow the error), a failed session is rolled back,
  the failure is recorded on the account from a fresh session, and every task
  outcome is inspected rather than discarded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

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

# Cycles that outlived the round watchdog and are still running. Strong
# references, so they cannot be garbage collected mid-flight.
_stragglers: set[asyncio.Task] = set()

# Customer-facing (and safe) text stored when a cycle could not even save its state.
CYCLE_FAILED_MESSAGE = (
    "The last check could not be completed because of a temporary internal problem. "
    "It will retry automatically on the next check."
)

OK, SKIPPED, FAILED = "ok", "skipped", "failed"


def _lock_for(account_id: str) -> asyncio.Lock:
    lock = _account_locks.get(account_id)
    if lock is None:
        lock = _account_locks[account_id] = asyncio.Lock()
    return lock


def _log_failure(account_id: str, what: str, exc: BaseException) -> None:
    """Log a scheduler failure using ONLY the account id already in hand.

    Never read the id (or anything) from an ORM object here: after a failed
    flush the session's objects are expired and unreadable until a rollback,
    and `account.id` inside the old `except` block raised, so the original
    error was never logged and vanished into `gather`."""
    # The phrase "error during ..." is what operators already grep the journal for.
    logger.error("account %s: error during %s: %s: %s", account_id, what, type(exc).__name__, exc, exc_info=exc)


def _persist_cycle_failure(account_id: str, *, scanned: bool) -> None:
    """Best effort, on a FRESH session: leave evidence on the account that its
    cycle ran but could not save normally. Never raises."""
    now = datetime.now(timezone.utc)
    try:
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, account_id)
            if account is not None:
                if scanned:
                    account.last_scanned_at = now
                account.last_error = CYCLE_FAILED_MESSAGE
                account.last_error_at = now
    except Exception as exc:
        _log_failure(account_id, "recording the failure on the account", exc)


async def _guarded_cycle(account_id: str, settings: Settings, work, *, label: str, require_active: bool, scanned: bool) -> str:
    """Run `work(session, account, notifier)` for one account in its own
    transaction scope. Never raises (except cancellation). Returns OK, SKIPPED
    or FAILED."""
    outcome = OK
    needs_record = False
    try:
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, account_id)
            if account is None or (require_active and not account.is_active):
                return SKIPPED
            channel = account.notification_channel
            try:
                notifier = build_notifier(
                    channel,
                    telegram_bot_token=settings.telegram_bot_token,
                    whatsapp_provider_token=settings.whatsapp_provider_token,
                )
            except Exception as exc:
                _log_failure(account_id, f"building the notification channel ({channel})", exc)
                return FAILED
            try:
                await work(session, account, notifier)
            except Exception as exc:
                outcome = FAILED
                _log_failure(account_id, label, exc)
                if not session.is_active:
                    # A failed flush/commit leaves the transaction unusable until rolled back.
                    session.rollback()
                    needs_record = True
    except Exception as exc:  # session setup, or the commit when the scope exits (e.g. "database is locked")
        outcome = FAILED
        needs_record = True
        _log_failure(account_id, f"saving the state after {label}", exc)
    if needs_record:
        _persist_cycle_failure(account_id, scanned=scanned)
    return outcome


async def _run_one(account_id: str, settings: Settings, slots: asyncio.Semaphore | None = None) -> str:
    # The per-account lock first, the shared slot second: waiting for a busy
    # account must never hold a slot another account could use.
    async with _lock_for(account_id):
        if slots is None:
            return await _run_one_locked(account_id, settings)
        async with slots:
            return await _run_one_locked(account_id, settings)


async def _run_one_locked(account_id: str, settings: Settings) -> str:
    async def work(session, account, notifier):
        await process_account(session, account, settings, notifier)

    return await _guarded_cycle(
        account_id, settings, work, label="processing cycle", require_active=True, scanned=True
    )


def _report_finished(account_id: str, task: asyncio.Task, label: str) -> str:
    """Inspect a finished cycle task - its outcome is never discarded."""
    if task.cancelled():
        logger.warning("account %s: %s was cancelled", account_id, label)
        return FAILED
    exc = task.exception()
    if exc is not None:  # _guarded_cycle never raises, so this is a bug if it ever happens
        _log_failure(account_id, label, exc)
        return FAILED
    return task.result()


def _straggler_finished(account_id: str, label: str, task: asyncio.Task) -> None:
    _stragglers.discard(task)
    outcome = _report_finished(account_id, task, label)
    logger.warning("account %s: the %s that exceeded the watchdog has now finished (%s)", account_id, label, outcome)


async def _wait_round(tasks: dict[asyncio.Task, str], *, timeout: float, label: str) -> dict[str, int]:
    """Wait for a round of per-account tasks, but only up to `timeout`. A task
    that is still running is reported and left to finish (never cancelled: it
    may be mid-order); it no longer holds up the round."""
    counts = {OK: 0, SKIPPED: 0, FAILED: 0, "running": 0}
    if not tasks:
        return counts
    done, pending = await asyncio.wait(tasks.keys(), timeout=timeout)
    for task in done:
        counts[_report_finished(tasks[task], task, label)] += 1
    for task in pending:
        account_id = tasks[task]
        counts["running"] += 1
        logger.warning(
            "account %s: %s still running after %.0fs; other accounts carry on and this one is skipped until it finishes",
            account_id, label, timeout,
        )
        _stragglers.add(task)
        task.add_done_callback(lambda t, a=account_id, lb=label: _straggler_finished(a, lb, t))
    return counts


async def run_once(settings: Settings) -> None:
    with session_scope() as session:
        account_ids = [a.id for a in list_active_accounts(session)]

    if not account_ids:
        logger.info("no active accounts to process")
        return

    started = time.monotonic()
    slots = asyncio.Semaphore(max(1, settings.max_concurrent_cycles))
    tasks: dict[asyncio.Task, str] = {}
    for account_id in account_ids:
        if _lock_for(account_id).locked():
            logger.warning("account %s: its previous cycle is still running, skipping this round", account_id)
            continue
        tasks[asyncio.ensure_future(_run_one(account_id, settings, slots))] = account_id

    counts = await _wait_round(tasks, timeout=settings.cycle_watchdog_seconds, label="processing cycle")
    level = logging.WARNING if counts[FAILED] or counts["running"] else logging.INFO
    logger.log(
        level,
        "scheduler round complete: %d active account(s), %d ok, %d failed, %d still running, %d skipped, %.1fs",
        len(account_ids), counts[OK], counts[FAILED], counts["running"], len(account_ids) - len(tasks), time.monotonic() - started,
    )


async def _recover_one(
    account_id: str, settings: Settings, *, current_price: float | None = None, slots: asyncio.Semaphore | None = None
) -> str:
    async def work(session, account, notifier):
        await reconcile_account(session, account, settings, notifier, current_price=current_price)

    async with _lock_for(account_id):
        if slots is None:
            # Not require_active: an open position is managed even on an account switched off.
            return await _guarded_cycle(account_id, settings, work, label="reconciliation", require_active=False, scanned=False)
        async with slots:
            return await _guarded_cycle(account_id, settings, work, label="reconciliation", require_active=False, scanned=False)


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
        slots = asyncio.Semaphore(max(1, settings.max_concurrent_cycles))
        tasks = {asyncio.ensure_future(_recover_one(a, settings, slots=slots)): a for a in account_ids}
        await _wait_round(tasks, timeout=settings.cycle_watchdog_seconds, label="reconciliation")


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
        slots = asyncio.Semaphore(max(1, settings.max_concurrent_cycles))
        tasks = {asyncio.ensure_future(_recover_one(a, settings, current_price=p, slots=slots)): a for a, p in due}
        await _wait_round(tasks, timeout=settings.cycle_watchdog_seconds, label="stop-limit reconciliation")


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


async def _supervised(name: str, make_loop) -> None:
    """Run a never-ending loop and restart it if it ever dies with an ordinary
    exception (they already catch their own; this is the last line of defence,
    and it logs - a dead scheduler must never be a silent one)."""
    while True:
        try:
            await make_loop()
        except Exception:
            logger.exception("the scheduler's %s stopped unexpectedly; restarting it in 5s", name)
            await asyncio.sleep(5)
        else:
            logger.error("the scheduler's %s returned unexpectedly; restarting it", name)


async def run_forever(settings: Settings) -> None:
    # Startup reconciliation: before the first poll, bring every live trade
    # in line with Binance. SQLite alone is never trusted after a restart.
    try:
        await recover_once(settings, include_healthy=True)
    except Exception:
        logger.exception("startup reconciliation failed")
    await asyncio.gather(
        _supervised("poll loop", lambda: run_poll_forever(settings)),
        _supervised("recovery loop", lambda: run_recovery_forever(settings)),
    )
