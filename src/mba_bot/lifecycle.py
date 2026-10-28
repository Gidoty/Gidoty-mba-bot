"""The live-trade state machine: write-ahead entry, exchange-side
protection, idempotent exit, and reconciliation against Binance.

`trader.py` decides WHEN to enter or exit (signals, sizing, safety gates);
this module owns HOW a real position is opened, protected, monitored and
closed, and is the only place that moves a live `Trade` between states:

    pending ──entry filled──▶ protecting ──stop verified──▶ open
       │                          │                           │
       │ no fill                  │ cannot protect            │ exit / stop fired
       ▼                          ▼                           ▼
     void                 emergency close ──────────▶ closing ──▶ closed
                                  │ cannot verify flat         ▲
                                  ▼                            │
                              attention ───reconciled──────────┘

Invariants (each has tests in tests/test_protective_lifecycle.py):

* Intent is committed BEFORE any order is sent; an entry whose outcome is
  unknown is never re-sent, and blocks every other entry for the account
  (the partial unique index on trades enforces this in the database).
* Client order ids are a pure function of (trade id, role, attempt), so any
  retry looks up / duplicates nothing.
* Only `execute_exit` closes exposure, and it first claims the trade
  atomically (status -> closing), cancels and confirms the protective stop,
  then re-reads exchange truth and closes only what is actually left.
* Futures exits are reduce-only; Spot never sells more than the bot's
  remaining quantity or the free balance.
* A live trade is only "open" once its protective stop has been placed AND
  read back from Binance.
* Nothing here swallows an unknown: when exchange state cannot be read the
  trade stays where it is and the next reconciliation tries again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt.async_support as ccxt_async
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import venue as venue_mod
from .config import Settings
from .models import (
    PROTECTION_CANCELLED,
    PROTECTION_MISSING,
    PROTECTION_PENDING,
    PROTECTION_PROTECTED,
    PROTECTION_TRIGGERED,
    TRADE_ATTENTION,
    TRADE_CLOSED,
    TRADE_CLOSING,
    TRADE_OPEN,
    TRADE_PENDING,
    TRADE_PROTECTING,
    TRADE_VOID,
    Trade,
    TradingAccount,
)
from .notifications import Notifier
from .venue import (
    CLIENT_ID_PREFIX,
    KIND_SPOT_LIMIT,
    ProtectionOrder,
    ProtectionUnavailable,
    Venue,
)

logger = logging.getLogger(__name__)

_sleep = venue_mod._sleep  # re-bound by tests via monkeypatch of this module attribute

MAX_EXIT_ATTEMPTS = 5

# Attention reasons (stored on Trade.attention_reason, never shown raw).
ATTN_UNPROTECTED = "unprotected_position"
ATTN_ENTRY_UNRESOLVED = "entry_unresolved"
ATTN_ORPHAN_POSITION = "orphan_position"
ATTN_SIDE_MISMATCH = "position_side_mismatch"
ATTN_EXIT_FAILED = "exit_failed"
ATTN_RESIDUAL = "residual_exposure"
ATTN_CANNOT_VERIFY = "cannot_verify_exchange"
ATTN_CANNOT_CANCEL = "cannot_cancel_protection"


class EntryRefused(RuntimeError):
    """A pre-entry safety check failed; no order was placed. `.message` is
    customer-safe."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class LiveContext:
    session: Session
    account: TradingAccount
    venue: Venue
    settings: Settings
    notifier: Notifier
    now: datetime


# --- ids / small helpers --------------------------------------------------------


def client_id(trade: Trade, role: str, attempt: int = 0) -> str:
    """Deterministic Binance client order id (<= 36 chars, [A-Za-z0-9]):
    entry `mba<24 hex>e`, stop `...s<n>`, exit `...x<n>`."""
    base = CLIENT_ID_PREFIX + trade.id.replace("-", "")[:24]
    if role == "entry":
        return base + "e"
    if role == "stop":
        return f"{base}s{attempt}"
    if role == "exit":
        return f"{base}x{attempt}"
    raise ValueError(role)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pair(ctx: LiveContext, trade: Trade) -> str:
    return trade.pair or ctx.account.pair


_DEFINITIVE_REJECTIONS = tuple(
    getattr(ccxt_async, name)
    for name in (
        "InsufficientFunds",
        "InvalidOrder",
        "BadRequest",
        "PermissionDenied",
        "AuthenticationError",
        "AccountSuspended",
        "NotSupported",
    )
    if hasattr(ccxt_async, name)
)


def is_definitive_rejection(exc: BaseException) -> bool:
    """True for errors where Binance clearly refused the request (nothing
    was created). Everything else - timeouts, 5xx, a generic ExchangeError,
    or a non-ccxt exception - leaves the outcome UNKNOWN."""
    if isinstance(exc, ccxt_async.NetworkError):
        return False
    return isinstance(exc, _DEFINITIVE_REJECTIONS)


def _cas(session: Session, trade: Trade, expected: tuple[str, ...], new: str, **fields) -> bool:
    """Atomic compare-and-set on trade.status. Returns False (and refreshes
    `trade`) if another worker already moved it. Commits - this IS the
    durable checkpoint."""
    try:
        result = session.execute(
            update(Trade).where(Trade.id == trade.id, Trade.status.in_(expected)).values(status=new, **fields)
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(trade)
    return result.rowcount == 1


def _set(session: Session, trade: Trade, **fields) -> None:
    """Unconditional field update + commit (for non-status fields)."""
    for key, value in fields.items():
        setattr(trade, key, value)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


async def _notify(ctx: LiveContext, message: str) -> None:
    if ctx.account.notification_target is None:
        return
    try:
        await ctx.notifier.send(ctx.account.notification_target, message)
    except Exception:
        logger.exception("account %s: failed to send a safety notification", ctx.account.id)


def _pnl(side: str, entry: float, exit_: float, qty: float) -> float:
    return (exit_ - entry) * qty if side == "long" else (entry - exit_) * qty


# --- entry --------------------------------------------------------------------------


async def preflight_entry(ctx: LiveContext) -> None:
    """Read-only checks that must pass before a live entry is even
    recorded. Raises EntryRefused (customer-safe message) or lets a
    network error propagate (fail closed: no entry this cycle).

    * Futures: the contract must be FLAT. A pre-existing position (the
      customer's own, or one this database lost track of) would be covered
      by a closePosition stop and could be stacked on by the entry.
    * No bot-prefixed order may be resting: a stale conditional left over
      from an earlier trade could later close a NEW position.
    """
    venue = ctx.venue
    if venue.market_type == "futures":
        position = await venue.signed_position()  # type: ignore[attr-defined]
        if abs(position) > 0:
            raise EntryRefused(
                "There is already an open position on this pair in your Binance Futures account that the bot "
                "did not open, so no new trade was started."
            )
    stale = [(cid, cond) for cid, cond in await venue.list_open_bot_orders()]
    for cid, cond in stale:
        logger.warning("account %s: cancelling stale bot order before entry", ctx.account.id)
        await venue.cancel_by_client_id(cid, conditional=cond)
    if stale:
        remaining = await venue.list_open_bot_orders()
        if remaining:
            raise EntryRefused("A leftover order from an earlier trade could not be cleared, so no new trade was started.")


async def open_live_trade(
    ctx: LiveContext,
    *,
    side: str,
    quantity: float,
    signal_price: float,
    stop_price: float,
    risk_text: str,
) -> Trade | None:
    """Write-ahead -> entry order -> resolve fill -> protect -> open.
    Returns the Trade (any state) or None if the write-ahead insert lost to
    another non-terminal trade (the database invariant)."""
    session, account, venue = ctx.session, ctx.account, ctx.venue
    if not venue.entries_allowed:
        raise venue_mod.EntryForbidden("open_live_trade requires an entry-capable venue")
    stop = venue.round_stop(stop_price, side=side)
    trade = Trade(
        account_id=account.id,
        side=side,
        is_paper=False,
        entry_price=signal_price,
        quantity=quantity,
        stop_loss_price=stop,
        status=TRADE_PENDING,
        opened_at=ctx.now,
        pair=account.pair,
        protection_status=PROTECTION_PENDING,
        protection_attempt=0,
        exit_attempt=0,
    )
    session.add(trade)
    try:
        session.commit()  # WRITE-AHEAD: the intent exists durably before any order does.
    except IntegrityError:
        session.rollback()
        logger.warning("account %s: a non-terminal trade already exists, not entering", account.id)
        return None
    except Exception:
        session.rollback()
        raise

    cid = client_id(trade, "entry")
    try:
        order = await venue.place_entry(side, quantity, cid)
    except Exception as exc:
        return await _resolve_entry_submission_error(ctx, trade, exc, risk_text=risk_text)
    return await _after_entry(ctx, trade, order, risk_text=risk_text)


async def _resolve_entry_submission_error(ctx: LiveContext, trade: Trade, exc: BaseException, *, risk_text: str) -> Trade:
    cid = client_id(trade, "entry")
    logger.warning("account %s: entry submission raised %s; checking Binance for the order", ctx.account.id, type(exc).__name__)
    try:
        found = await ctx.venue.find_order(cid)
    except Exception:
        # Cannot tell whether it exists. NEVER re-send; leave it pending for the reconciler.
        logger.warning("account %s: entry outcome unknown, leaving trade pending", ctx.account.id)
        return trade
    if found is not None:
        return await _after_entry(ctx, trade, found, risk_text=risk_text)
    if is_definitive_rejection(exc):
        _cas(ctx.session, trade, (TRADE_PENDING,), TRADE_VOID, closed_at=ctx.now, close_reason="entry_rejected")
        raise exc
    # Timed out / unreadable and Binance has no record YET: it may still be
    # in flight. Leave it pending; reconcile_trade voids it only once it is
    # old enough that it cannot still arrive.
    return trade


async def _after_entry(ctx: LiveContext, trade: Trade, order: venue_mod.OrderInfo, *, risk_text: str = "") -> Trade:
    venue, session = ctx.venue, ctx.session
    cid = client_id(trade, "entry")
    if not order.is_terminal or (order.status == "closed" and order.filled <= 0):
        resolved = await venue.await_final(
            cid, attempts=ctx.settings.entry_resolve_attempts, delay=ctx.settings.entry_resolve_delay_seconds
        )
        if resolved is not None:
            order = resolved
    if not order.is_terminal:
        logger.warning("account %s: entry order not yet terminal, leaving trade pending", ctx.account.id)
        return trade  # market orders should not rest; the reconciler keeps polling
    if order.filled <= 0:
        _cas(session, trade, (TRADE_PENDING, TRADE_ATTENTION), TRADE_VOID, closed_at=ctx.now, close_reason="entry_not_filled", entry_order_id=order.order_id)
        return trade

    # Quantity the bot holds from this fill. Futures: the filled size. Spot:
    # filled minus the base-asset commission, but ONLY from exchange-confirmed
    # data. If the order response carried no commission information the net
    # quantity is not yet known: record the gross fill with entry_fee_base
    # unset, and _protect_or_emergency resolves it from the fill records (or
    # takes the emergency path) before any stop is sized. Nothing is assumed.
    held = order.filled
    fee_base: float | None = None
    if venue.market_type == "spot" and order.fee_known:
        fee_base = order.fee_base
        held = venue.net_from_fee(order.filled, fee_base)  # type: ignore[attr-defined]
    entry_price = order.average or trade.entry_price
    moved = _cas(
        session,
        trade,
        (TRADE_PENDING, TRADE_ATTENTION),
        TRADE_PROTECTING,
        entry_order_id=order.order_id,
        entry_price=entry_price,
        quantity=held,
        entry_fee_base=fee_base,
        attention_reason=None,
    )
    if not moved and trade.status not in (TRADE_PROTECTING, TRADE_OPEN):
        return trade
    if trade.status == TRADE_OPEN:
        return trade
    return await _protect_or_emergency(ctx, trade, risk_text=risk_text, announce_entry=True)


# --- protection ---------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    return not is_definitive_rejection(exc)


async def _place_and_verify(ctx: LiveContext, trade: Trade) -> str:
    """One protection attempt. Returns 'protected' or 'executed' (the stop
    already fired). Raises on any failure."""
    venue, session = ctx.venue, ctx.session
    if not trade.protection_attempt:
        _set(session, trade, protection_attempt=1)
    cid = client_id(trade, "stop", trade.protection_attempt)

    existing = await venue.get_protection(kind=trade.protective_kind, client_id=cid)
    if existing is not None:
        if existing.is_resting:
            return _adopt_protection(ctx, trade, existing)
        if existing.state in ("filled", "triggered") or existing.filled > 0:
            return "executed"
        # Terminal with nothing executed: this attempt is burned; use a fresh id.
        _set(session, trade, protection_attempt=trade.protection_attempt + 1)
        cid = client_id(trade, "stop", trade.protection_attempt)

    # Bound the quantity by what the exchange confirms is available: never
    # more than the bot-attributable quantity, never more than the free
    # balance (the wallet may hold the customer's own units too, so free is
    # only an upper bound, never the amount to protect).
    protectable = await venue.protectable_quantity(trade.quantity)
    if protectable < trade.quantity:
        logger.warning("account %s: available balance below the expected quantity, protecting what is there", ctx.account.id)
        _set(session, trade, quantity=protectable)
    await venue.protection_precheck(side=trade.side, stop_price=trade.stop_loss_price)
    try:
        placed = await venue.place_protection(
            side=trade.side, quantity=trade.quantity, stop_price=trade.stop_loss_price, client_id=cid, entry_price=trade.entry_price
        )
    except Exception as exc:
        if is_definitive_rejection(exc) or isinstance(exc, ProtectionUnavailable):
            _set(session, trade, protection_attempt=trade.protection_attempt + 1)
        raise
    verified = await venue.get_protection(kind=placed.kind, client_id=cid)
    if verified is None:
        raise RuntimeError("protective order could not be read back")
    if verified.is_resting:
        return _adopt_protection(ctx, trade, verified)
    if verified.state in ("filled", "triggered") or verified.filled > 0:
        return "executed"
    raise RuntimeError("protective order is not resting after placement")


def _adopt_protection(ctx: LiveContext, trade: Trade, prot: ProtectionOrder) -> str:
    moved = _cas(
        ctx.session,
        trade,
        (TRADE_PROTECTING, TRADE_OPEN, TRADE_ATTENTION),
        TRADE_OPEN,
        protective_order_id=prot.order_id,
        protective_kind=prot.kind,
        protective_limit_price=prot.limit_price,
        protection_status=PROTECTION_PROTECTED,
        protection_verified_at=ctx.now,
        attention_reason=None,
    )
    if not moved:
        # Someone else moved the trade (e.g. an exit lease): this stop is
        # not ours to declare "protected".
        raise RuntimeError("trade changed state while verifying its protective order")
    return "protected"


async def establish_protection(ctx: LiveContext, trade: Trade) -> str:
    """Bounded attempts to get a verified resting stop. Returns
    'protected', 'executed', or 'failed'. Deterministic rejections stop
    retrying immediately."""
    attempts = max(1, ctx.settings.protection_max_attempts)
    for i in range(attempts):
        try:
            return await _place_and_verify(ctx, trade)
        except Exception as exc:
            # ProtectionUnavailable carries one of this codebase's own fixed reasons (never exchange text), so it is
            # safe to log and says which check refused; every other exception logs its type only, as before.
            logger.warning(
                "account %s: protective stop attempt %d/%d failed (%s%s)", ctx.account.id, i + 1, attempts,
                type(exc).__name__, f": {exc}" if isinstance(exc, ProtectionUnavailable) and str(exc) else "",
            )
            # An InsufficientFunds from the exchange means the free balance moved
            # between our read and the order: retrying re-reads it and re-bounds
            # the quantity (the attempt id was already advanced).
            retryable = _is_transient(exc) or isinstance(exc, ccxt_async.InsufficientFunds)
            if not retryable or isinstance(exc, ProtectionUnavailable):
                break
            if i + 1 < attempts:
                await _sleep(ctx.settings.protection_retry_delay_seconds)
    return "failed"


async def _protect_or_emergency(
    ctx: LiveContext, trade: Trade, *, risk_text: str = "", announce_entry: bool = False, was_attention: bool = False
) -> Trade:
    if ctx.venue.market_type == "spot" and trade.entry_order_id and trade.entry_fee_base is None:
        if not await _resolve_spot_quantity(ctx, trade):
            return await emergency_close(
                ctx, trade, reason="quantity_unverifiable",
                why="Binance's fill details for this trade could not be confirmed, so no protective stop could be "
                "sized safely",
            )

    # Spot dust: a fill too small to place ANY stop on is also too small to
    # sell; there is nothing the exchange would let us do about it.
    if (
        ctx.venue.market_type == "spot"
        and not ctx.venue.can_protect(trade.quantity, trade.stop_loss_price)  # type: ignore[attr-defined]
        and not ctx.venue.can_sell(trade.quantity, trade.entry_price)  # type: ignore[attr-defined]
    ):
        _cas(
            ctx.session, trade, (TRADE_PROTECTING, TRADE_ATTENTION), TRADE_CLOSED,
            closed_at=ctx.now, close_reason="dust_unprotectable", exit_price=trade.entry_price, pnl=0.0,
            protection_status=PROTECTION_CANCELLED,
        )
        await _notify(ctx, "[LIVE] A tiny partial fill was below Binance's minimum tradable size, so no stop could be placed on it. No trade is open.")
        return trade

    outcome = await establish_protection(ctx, trade)
    if outcome == "protected":
        if was_attention:
            await _clear_attention(ctx, trade)
        if announce_entry:
            await _notify(
                ctx,
                f"[LIVE] Entered {trade.side.upper()} {_pair(ctx, trade)}\n"
                f"Entry: {trade.entry_price:.4f}  Qty: {trade.quantity:.6f}  Stop: {trade.stop_loss_price:.4f} "
                f"(resting at Binance)\n{risk_text}".rstrip(),
            )
        return trade
    if outcome == "executed":
        await finalize_exchange_close(ctx, trade, reason="exchange_stop_triggered")
        return trade
    return await emergency_close(ctx, trade)


async def _resolve_spot_quantity(ctx: LiveContext, trade: Trade) -> bool:
    """Establish the bot-attributable base quantity of a Spot fill from
    exchange-confirmed commission data (see SpotVenue.resolve_entry_fee_base)
    and persist it. Bounded retries; returns False if it cannot be
    established, which sends the caller to the emergency path."""
    venue = ctx.venue
    attempts = max(1, ctx.settings.protection_max_attempts)
    for i in range(attempts):
        try:
            order = await venue.find_order(client_id(trade, "entry"))
            if order is None or not order.is_terminal or order.filled <= 0:
                raise venue_mod.QuantityUnresolvable("entry order state unavailable")
            fee_base = await venue.resolve_entry_fee_base(order)  # type: ignore[attr-defined]
        except (venue_mod.QuantityUnresolvable, ccxt_async.NetworkError, ccxt_async.ExchangeError) as exc:
            logger.warning("account %s: spot quantity attempt %d/%d unresolved (%s)", ctx.account.id, i + 1, attempts, type(exc).__name__)
            if i + 1 < attempts:
                await _sleep(ctx.settings.protection_retry_delay_seconds)
            continue
        _set(
            ctx.session, trade,
            entry_fee_base=fee_base,
            quantity=venue.net_from_fee(order.filled, fee_base),  # type: ignore[attr-defined]
        )
        return True
    return False


async def emergency_close(
    ctx: LiveContext, trade: Trade, *, reason: str = "protection_failed_emergency_exit", why: str | None = None
) -> Trade:
    """Protection could not be established. Close the exposure now rather
    than leave it unprotected; if even that cannot be verified, go to
    ATTENTION and keep working on it."""
    result = await execute_exit(ctx, trade, reason=reason, emergency=True)
    if result == "closed":
        why = why or "Binance would not accept the protective stop for this trade"
        await _notify(
            ctx,
            f"[LIVE] Safety exit on {_pair(ctx, trade)}: {why}, "
            f"so the bot closed the position immediately instead of leaving it unprotected.\n"
            f"Entry: {trade.entry_price:.4f}  Exit: {trade.exit_price:.4f}  P&L: {trade.pnl:+.2f}",
        )
        return trade
    await mark_attention(ctx, trade, ATTN_UNPROTECTED)
    return trade


async def mark_attention(ctx: LiveContext, trade: Trade, reason: str) -> None:
    """Move to ATTENTION and alert ONCE per episode. The episode is
    remembered by `attention_reason` (set here, cleared whenever the trade
    is resolved), not by status, because recovery attempts legitimately
    pass through `closing`/`protecting` on the way back to attention."""
    first_alert = trade.attention_reason is None
    if trade.status in (TRADE_CLOSED, TRADE_VOID):
        return
    _cas(
        ctx.session, trade, (TRADE_PENDING, TRADE_PROTECTING, TRADE_OPEN, TRADE_CLOSING, TRADE_ATTENTION), TRADE_ATTENTION,
        attention_reason=reason,
        protection_status=PROTECTION_MISSING if reason == ATTN_UNPROTECTED else trade.protection_status,
    )
    if first_alert:
        await _notify(
            ctx,
            f"ATTENTION: your MBA Bot needs a safety check on your {_pair(ctx, trade)} position. New trades are "
            "on hold until it is resolved. The bot is retrying automatically; please also check your Binance account.",
        )


async def _clear_attention(ctx: LiveContext, trade: Trade) -> None:
    await _notify(ctx, "Resolved: your MBA Bot safety check is clear and normal monitoring has resumed.")


# --- exit ------------------------------------------------------------------------------


async def _exit_fills(ctx: LiveContext, trade: Trade) -> list[tuple[float, float]]:
    """(quantity, price) of everything that has already exited this trade,
    from the exchange's own records."""
    venue = ctx.venue
    if venue.market_type == "futures":
        # 2 minutes of slack for clock skew; realized_exit takes newest fills first.
        since = int(_aware(trade.opened_at).timestamp() * 1000) - 120_000 if trade.opened_at else 0
        realized = await venue.realized_exit(side=trade.side, since_ms=since, max_quantity=trade.quantity)  # type: ignore[attr-defined]
        return [realized] if realized else []
    fills: list[tuple[float, float]] = []
    for n in range(1, (trade.protection_attempt or 0) + 1):
        prot = await venue.get_protection(kind=trade.protective_kind, client_id=client_id(trade, "stop", n))
        if prot is not None and prot.filled > 0:
            fills.append((prot.filled, prot.average or prot.limit_price or trade.stop_loss_price))
    for n in range(1, (trade.exit_attempt or 0) + 1):
        order = await venue.find_order(client_id(trade, "exit", n))
        if order is not None and order.filled > 0:
            fills.append((order.filled, order.average or trade.stop_loss_price))
    return fills


async def _finalize(ctx: LiveContext, trade: Trade, *, reason: str, fallback_price: float, protection: str) -> bool:
    fills = await _exit_fills(ctx, trade)
    qty = sum(q for q, _ in fills)
    if qty > 0:
        exit_price = sum(q * p for q, p in fills) / qty
    else:
        exit_price, qty = fallback_price, trade.quantity
    pnl = _pnl(trade.side, trade.entry_price, exit_price, qty)
    moved = _cas(
        ctx.session, trade, (TRADE_OPEN, TRADE_PROTECTING, TRADE_ATTENTION, TRADE_CLOSING), TRADE_CLOSED,
        exit_price=exit_price, pnl=pnl, closed_at=ctx.now, close_reason=reason,
        protection_status=protection, attention_reason=None,
    )
    return moved


async def finalize_exchange_close(ctx: LiveContext, trade: Trade, *, reason: str) -> None:
    """The exposure is already gone on the exchange (stop fired, or closed
    outside the bot). Record it - never send another order."""
    was_attention = trade.status == TRADE_ATTENTION or trade.attention_reason is not None
    # Make sure no resting bot order survives to affect a future position.
    try:
        for cid, cond in await ctx.venue.list_open_bot_orders():
            await ctx.venue.cancel_by_client_id(cid, conditional=cond)
    except Exception:
        logger.warning("account %s: could not clear leftover orders after close", ctx.account.id)
    protection = PROTECTION_TRIGGERED if reason == "exchange_stop_triggered" else PROTECTION_CANCELLED
    if await _finalize(ctx, trade, reason=reason, fallback_price=trade.stop_loss_price, protection=protection):
        label = "stop-loss triggered at Binance" if reason == "exchange_stop_triggered" else "closed on Binance"
        await _notify(
            ctx,
            f"[LIVE] Exited {trade.side.upper()} {_pair(ctx, trade)} ({label})\n"
            f"Entry: {trade.entry_price:.4f}  Exit: {trade.exit_price:.4f}  P&L: {trade.pnl:+.2f}",
        )
        if was_attention:
            await _clear_attention(ctx, trade)


async def execute_exit(
    ctx: LiveContext, trade: Trade, *, reason: str, emergency: bool = False, price_hint: float | None = None
) -> str:
    """The ONLY path that closes live exposure. Returns 'closed', 'busy'
    (another worker holds the lease), or 'deferred' (could not finish
    safely now; trade stays `closing`/`attention`, retried by reconcile)."""
    venue, session = ctx.venue, ctx.session
    was_attention = trade.status == TRADE_ATTENTION or trade.attention_reason is not None

    if trade.status in (TRADE_OPEN, TRADE_PROTECTING, TRADE_ATTENTION):
        if not _cas(session, trade, (TRADE_OPEN, TRADE_PROTECTING, TRADE_ATTENTION), TRADE_CLOSING, close_reason=reason):
            return "busy"
    elif trade.status != TRADE_CLOSING:
        return "busy"  # pending/closed/void: nothing this path may do
    reason = trade.close_reason or reason

    # -- 1. take the protective stop out of play, and learn if it already ran
    stop_executed = False
    if trade.protective_order_id or trade.protective_kind or (trade.protection_attempt or 0) > 0:
        stop_cid = client_id(trade, "stop", trade.protection_attempt or 1)
        try:
            final = await venue.cancel_protection(kind=trade.protective_kind or venue.default_kind, client_id=stop_cid)
        except Exception as exc:
            logger.warning("account %s: could not confirm protective order cancelled (%s)", ctx.account.id, type(exc).__name__)
            if venue.market_type == "spot":
                # A resting stop still reserves the base asset and may still
                # fire: selling on top of it is exactly the double exit.
                return "deferred"
            final = None  # Futures: reduceOnly makes the exit safe regardless
        stop_executed = final is not None and (final.state in ("filled", "triggered") or final.filled > 0)
    # -- 2. exchange truth
    try:
        if venue.market_type == "futures":
            signed = await venue.signed_position()  # type: ignore[attr-defined]
            want = 1.0 if trade.side == "long" else -1.0
            if signed != 0 and (signed > 0) != (want > 0):
                await mark_attention(ctx, trade, ATTN_SIDE_MISMATCH)
                return "deferred"
            remaining = min(abs(signed), trade.quantity)
        else:
            sold = sum(q for q, _ in await _exit_fills(ctx, trade))
            expected = max(trade.quantity - sold, 0.0)
            remaining = min(expected, await venue.exposure())
    except Exception as exc:
        logger.warning("account %s: could not read exchange state for exit (%s)", ctx.account.id, type(exc).__name__)
        return "deferred"

    rules = venue._require_ready()
    exitable = venue_mod.floor_to_step(remaining, rules.market_step)
    if exitable < max(rules.market_min_qty, rules.min_qty) or exitable <= 0:
        # Nothing (above the exchange's tradable minimum) is left to close.
        return await _record_exit(
            ctx, trade, reason=reason, stop_executed=stop_executed, price_hint=price_hint, emergency=emergency, was_attention=was_attention
        )

    # -- 3. the exit order, deterministic and idempotent
    tries = max(1, ctx.settings.protection_max_attempts) if emergency else 1
    for t in range(tries):
        if not trade.exit_attempt:
            _set(session, trade, exit_attempt=1)
        if trade.exit_attempt > MAX_EXIT_ATTEMPTS:
            await mark_attention(ctx, trade, ATTN_EXIT_FAILED)
            return "deferred"
        cid = client_id(trade, "exit", trade.exit_attempt)
        try:
            order = await venue.find_order(cid)
            if order is None:
                order = await venue.place_exit(trade.side, exitable, cid)
            if not order.is_terminal:
                order = await venue.await_final(
                    cid, attempts=ctx.settings.entry_resolve_attempts, delay=ctx.settings.entry_resolve_delay_seconds
                ) or order
        except Exception as exc:
            logger.warning("account %s: exit order attempt failed (%s)", ctx.account.id, type(exc).__name__)
            if emergency and t + 1 < tries and _is_transient(exc):
                await _sleep(ctx.settings.protection_retry_delay_seconds)
                continue
            return "deferred"
        if not order.is_terminal:
            return "deferred"
        if order.filled <= 0:
            _set(session, trade, exit_attempt=trade.exit_attempt + 1)  # burned: fresh id next time
            if emergency and t + 1 < tries:
                continue
            return "deferred"
        _set(session, trade, exit_order_id=order.order_id)
        break
    else:
        return "deferred"

    # -- 4. verify, then record
    # What is still the bot's to close: the part of its own quantity not yet
    # closed, but never more than the exchange still holds (Futures position /
    # Spot free base). The exchange figure alone is NOT the test: it may
    # include the customer's own units (Spot) or an enlarged position
    # (Futures) that the bot must never touch, and on Spot the bot's stored
    # quantity can be a gross figure when the commission was unconfirmed.
    try:
        done = sum(q for q, _ in await _exit_fills(ctx, trade))
        left = min(max(trade.quantity - done, 0.0), await venue.exposure())
    except Exception:
        return "deferred"
    if venue_mod.floor_to_step(left, rules.market_step) >= max(rules.market_min_qty, rules.min_qty):
        _set(session, trade, exit_attempt=trade.exit_attempt + 1)  # partial: next pass closes the remainder with a fresh id
        return "deferred"
    return await _record_exit(
            ctx, trade, reason=reason, stop_executed=stop_executed, price_hint=price_hint, emergency=emergency, was_attention=was_attention
        )


async def _record_exit(
    ctx: LiveContext,
    trade: Trade,
    *,
    reason: str,
    stop_executed: bool,
    price_hint: float | None,
    emergency: bool,
    was_attention: bool = False,
) -> str:
    if stop_executed:
        reason = "exchange_stop_triggered"
    protection = PROTECTION_TRIGGERED if stop_executed else PROTECTION_CANCELLED
    if not await _finalize(ctx, trade, reason=reason, fallback_price=price_hint or trade.stop_loss_price, protection=protection):
        return "busy"
    if not emergency:
        label = {
            "exchange_stop_triggered": "stop-loss triggered at Binance",
            "stop_loss": "stop-loss",
            "stop_limit_gap": "stop-loss",
            "crossover_exit": "crossover exit",
        }.get(reason, "closed")
        await _notify(
            ctx,
            f"[LIVE] Exited {trade.side.upper()} {_pair(ctx, trade)} ({label})\n"
            f"Entry: {trade.entry_price:.4f}  Exit: {trade.exit_price:.4f}  P&L: {trade.pnl:+.2f}",
        )
    if was_attention:
        await _clear_attention(ctx, trade)
    return "closed"


# --- reconciliation -------------------------------------------------------------------


async def _protection_state(ctx: LiveContext, trade: Trade) -> ProtectionOrder | None:
    if (trade.protection_attempt or 0) <= 0:
        return None
    return await ctx.venue.get_protection(
        kind=trade.protective_kind, client_id=client_id(trade, "stop", trade.protection_attempt)
    )


async def reconcile_trade(ctx: LiveContext, trade: Trade) -> Trade:
    """Bring one non-terminal LIVE trade in line with Binance. Safe to run
    any number of times, from startup, the main loop and the fast recovery
    loop. Returns the (possibly now terminal) trade."""
    venue = ctx.venue
    status = trade.status
    if status in (TRADE_CLOSED, TRADE_VOID):
        return trade

    if status == TRADE_PENDING or (status == TRADE_ATTENTION and trade.entry_order_id is None):
        # Entry never resolved to a known order: nothing here may be
        # adopted as "ours" until Binance shows the order by client id.
        return await _reconcile_pending(ctx, trade)

    if status == TRADE_PROTECTING:
        return await _recover_protection(ctx, trade)

    if status == TRADE_CLOSING:
        # A trade that is STILL closing on a later pass means the previous
        # attempt could not finish: surface it rather than loop silently.
        result = await execute_exit(ctx, trade, reason=trade.close_reason or "crossover_exit")
        if result == "deferred" and trade.status == TRADE_CLOSING:
            await mark_attention(ctx, trade, ATTN_EXIT_FAILED)
        return trade

    # open / attention: compare with exchange truth
    position: float | None = None
    if venue.market_type == "futures":
        position = await venue.signed_position()  # type: ignore[attr-defined]
        if position == 0:
            prot = await _protection_state(ctx, trade)
            fired = prot is not None and prot.state in ("filled", "triggered")
            await finalize_exchange_close(ctx, trade, reason="exchange_stop_triggered" if fired else "externally_closed")
            return trade
        want_long = trade.side == "long"
        if (position > 0) != want_long:
            await mark_attention(ctx, trade, ATTN_SIDE_MISMATCH)
            return trade

    prot = await _protection_state(ctx, trade)
    if prot is not None and prot.is_resting:
        if prot.state == "partial":
            return trade  # a triggered stop-limit is mid-fill; let it work
        _set(ctx.session, trade, protection_verified_at=ctx.now, protection_status=PROTECTION_PROTECTED)
        if trade.status == TRADE_ATTENTION:
            _cas(ctx.session, trade, (TRADE_ATTENTION,), TRADE_OPEN, attention_reason=None)
            await _clear_attention(ctx, trade)
        await _cleanup_stale(ctx, trade)
        return trade

    if prot is not None and (prot.state in ("filled", "triggered") or prot.filled > 0):
        if venue.market_type == "futures":
            # triggered but a position remains: finish closing what is left
            await execute_exit(ctx, trade, reason="stop_loss")
            return trade
        sold = prot.filled
        if sold >= trade.quantity - venue._require_ready().step:
            await finalize_exchange_close(ctx, trade, reason="exchange_stop_triggered")
        else:
            await execute_exit(ctx, trade, reason="stop_loss")
        return trade

    # Protection is absent or was cancelled with nothing executed.
    if venue.market_type == "spot":
        free = await venue.exposure()
        rules = venue._require_ready()
        if free < rules.min_qty:
            await finalize_exchange_close(ctx, trade, reason="externally_closed")
            return trade
    return await _recover_protection(ctx, trade)


async def _recover_protection(ctx: LiveContext, trade: Trade) -> Trade:
    venue = ctx.venue
    if venue.market_type == "futures":
        position = await venue.signed_position()  # type: ignore[attr-defined]
        if position == 0:
            await finalize_exchange_close(ctx, trade, reason="externally_closed")
            return trade
    was_attention = trade.status == TRADE_ATTENTION or trade.attention_reason is not None
    if trade.status == TRADE_OPEN:
        _cas(ctx.session, trade, (TRADE_OPEN,), TRADE_PROTECTING, protection_status=PROTECTION_MISSING)
    return await _protect_or_emergency(ctx, trade, announce_entry=False, was_attention=was_attention)


async def _reconcile_pending(ctx: LiveContext, trade: Trade) -> Trade:
    venue = ctx.venue
    order = await venue.find_order(client_id(trade, "entry"))
    if order is not None:
        return await _after_entry(ctx, trade, order)
    age = (ctx.now - _aware(trade.opened_at)).total_seconds()
    if age < ctx.settings.entry_unknown_void_after_seconds:
        return trade  # may still be in flight at Binance
    if venue.market_type == "futures":
        position = await venue.signed_position()  # type: ignore[attr-defined]
        if position != 0:
            await mark_attention(ctx, trade, ATTN_ORPHAN_POSITION)
            return trade
    _cas(ctx.session, trade, (TRADE_PENDING, TRADE_ATTENTION), TRADE_VOID, closed_at=ctx.now, close_reason="entry_not_found")
    return trade


async def _cleanup_stale(ctx: LiveContext, trade: Trade) -> None:
    """Cancel bot-prefixed open orders that don't belong to this trade's
    current protective stop. Best effort: never fails the cycle."""
    try:
        keep = {client_id(trade, "stop", trade.protection_attempt or 0)}
        for cid, cond in await ctx.venue.list_open_bot_orders():
            if cid not in keep:
                logger.warning("account %s: cancelling stale bot order", ctx.account.id)
                await ctx.venue.cancel_by_client_id(cid, conditional=cond)
    except Exception as exc:
        logger.warning("account %s: stale-order cleanup skipped (%s)", ctx.account.id, type(exc).__name__)


async def monitor_trade(ctx: LiveContext, trade: Trade, *, crossover_exit: bool, current_price: float) -> Trade:
    """Per-cycle decisions on a healthy, protected live trade: the software
    backstop for a stop that has not fired when it should have, and the
    strategy's opposite-crossover exit. Both go through `execute_exit`."""
    if trade.status != TRADE_OPEN or trade.protection_status != PROTECTION_PROTECTED:
        return trade
    grace = ctx.settings.protection_software_grace_pct / 100.0
    if trade.protective_kind == KIND_SPOT_LIMIT and trade.protective_limit_price:
        # A stop-LIMIT that price has gapped through will never fill.
        gapped = current_price <= trade.protective_limit_price
        reason = "stop_limit_gap"
    elif trade.side == "long":
        gapped = current_price <= trade.stop_loss_price * (1.0 - grace)
        reason = "stop_loss"
    else:
        gapped = current_price >= trade.stop_loss_price * (1.0 + grace)
        reason = "stop_loss"
    if gapped:
        await execute_exit(ctx, trade, reason=reason, price_hint=current_price)
    elif crossover_exit:
        await execute_exit(ctx, trade, reason="crossover_exit", price_hint=current_price)
    return trade
