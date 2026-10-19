"""One evaluation cycle for one customer account.

`process_account` is the unit of work the scheduler fans out over every
active account each poll interval. It is written to fail closed and
isolated: any exception here is caught by the caller (scheduler.py) so one
customer's bad data or exchange error never stops another customer's loop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import exchange, lifecycle
from .config import SUPPORTED_PAIRS, Settings
from .crypto_utils import decrypt
from .db import get_open_trade, get_trades_closed_on
from .models import PROTECTION_PROTECTED, TRADE_OPEN, Trade, TradingAccount
from .notifications import Notifier
from .onboarding import TradingMode, resolve_trading_mode
from .pair_selection import PairCandidate, rank_candidates
from .risk import apply_exposure_ceiling, apply_margin_ceiling, compute_position_size, compute_stop_loss
from .strategy import SignalType, StrategySignal, candles_from_ohlcv, generate_signal
from .venue import ProtectionUnavailable, build_venue

logger = logging.getLogger(__name__)


async def process_account(session: Session, account: TradingAccount, settings: Settings, notifier: Notifier) -> None:
    if account.notification_target is None:
        logger.info("account %s: notification channel not linked yet, skipping until it is", account.id)
        return

    now = datetime.now(timezone.utc)
    # We attempted a real cycle for this account - record that regardless
    # of how the rest of this function goes, so "last checked" on the
    # dashboard reflects reality even during a run of errors.
    account.last_scanned_at = now

    trial_state = resolve_trading_mode(
        trial_ends_at=account.trial_ends_at,
        live_bypass_accepted_at=account.live_bypass_accepted_at,
        has_paid=account.customer.has_paid,
        live_trading_enabled_at=account.live_trading_enabled_at,
        has_mainnet_credential=account.has_mainnet_credential,
        bot_started_at=account.bot_started_at,
        now=now,
    )
    is_paper = trial_state.mode == TradingMode.PAPER

    if not is_paper and account.live_notified_at is None:
        account.live_notified_at = now
        await _notify_safe(
            notifier,
            account,
            f"Your MBA Bot is now live and actively scanning {account.pair} ({account.market_type}) for trade "
            "signals, using your own Binance funds. You'll hear from this chat the moment it opens or closes a trade.",
        )

    # An already-open trade's own `is_paper` - set once at entry and
    # never touched again - governs how THIS cycle manages it:
    # credentials, exchange environment, and whether its exit is a real
    # order. `is_paper` above (freshly recomputed every cycle from
    # resolve_trading_mode) only decides whether a NEW entry may go
    # live; it must never retroactively change which exchange/credential
    # an already-resting position is managed against. Without this
    # distinction, any gate that can newly turn a previously-LIVE
    # account to PAPER (the bot_started_at migration default being the
    # immediate case, but also e.g. a credential downgrade mid-trade)
    # would silently switch a real open position onto testnet
    # credentials/market data and skip its real closing order - leaving
    # it resting on Binance with zero stop-loss/exit monitoring.
    open_trade = get_open_trade(session, account.id)
    effective_is_paper = open_trade.is_paper if open_trade is not None else is_paper

    api_key, api_secret = _resolve_credentials(account, settings, is_paper=effective_is_paper)
    if api_key is None:
        logger.warning(
            "account %s has no usable credentials for mode=%s, skipping",
            account.id,
            "paper" if effective_is_paper else "live",
        )
        await _record_error(notifier, account, "No usable trading credentials are configured for this account.", now=now)
        return

    client = exchange.build_exchange_client(
        exchange="binance",
        market_type=account.market_type,
        api_key=api_key,
        api_secret=api_secret,
        sandbox=effective_is_paper,
    )
    try:
        if not effective_is_paper:
            await exchange.assert_trade_only(client)

        if open_trade is not None and not open_trade.is_paper:
            # A live position is reconciled against Binance FIRST, before
            # any market data is needed: restart recovery, a stop that
            # fired while we were offline, an unprotected or unresolved
            # state all get handled here, whatever the strategy says.
            open_trade, ctx = await _manage_live_trade(session, account, client, open_trade, settings, notifier, now=now)
            account.last_error = None
            account.last_error_at = None
            if open_trade.status != TRADE_OPEN or open_trade.protection_status != PROTECTION_PROTECTED:
                # Closed this cycle (back to scanning next cycle), or not
                # healthy and being worked by the reconciler: no strategy
                # decisions on a trade whose protection is not verified.
                await _maybe_send_daily_summary(session, account, notifier, now=now)
                return
            ohlcv = await client.fetch_ohlcv(
                exchange.exchange_symbol(open_trade.pair or account.pair, account.market_type),
                timeframe=settings.timeframe,
                limit=settings.ohlcv_limit,
            )
            candles = candles_from_ohlcv(ohlcv)
            if not candles:
                logger.warning("account %s: no OHLCV data returned for %s", account.id, account.pair)
                await _record_error(notifier, account, "No market data was returned for this account's pair just now.", now=now)
                return
            signal = generate_signal(
                candles,
                ema_fast_period=settings.ema_fast_period,
                ema_slow_period=settings.ema_slow_period,
                atr_period=settings.atr_period,
                atr_flat_threshold=settings.atr_flat_threshold,
                atr_rolling_window=settings.atr_rolling_window,
            )
            account.last_signal = signal.signal.value
            account.last_signal_at = now
            opposite_cross = (open_trade.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (
                open_trade.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER
            )
            await lifecycle.monitor_trade(ctx, open_trade, crossover_exit=opposite_cross, current_price=candles[-1].close)
            await _maybe_send_daily_summary(session, account, notifier, now=now)
            return

        # Auto Select only ever runs this scan when there is no open
        # trade (the `open_trade is None` check right here) - that, not
        # just the API-layer guard in api/routers/account.py, is what
        # makes "never switch pairs while a position is open" true in
        # the backend trading logic itself: there is no code path from
        # this branch into an account that has one.
        if open_trade is None and account.pair_selection_mode == "auto":
            auto_result = await _scan_auto_pair(client, account, settings)
            if auto_result is None:
                # Nothing in the approved universe had an actionable
                # signal this cycle - a normal, uneventful scan, not an
                # error. Record it as such and stop; there is nothing to
                # enter and no open trade to manage.
                account.last_signal = SignalType.NONE.value
                account.last_signal_at = now
                account.last_error = None
                account.last_error_at = None
                return
            scan_pair, candles, signal = auto_result
        else:
            scan_pair = account.pair
            ohlcv = await client.fetch_ohlcv(
                exchange.exchange_symbol(account.pair, account.market_type),
                timeframe=settings.timeframe,
                limit=settings.ohlcv_limit,
            )
            candles = candles_from_ohlcv(ohlcv)
            if not candles:
                logger.warning("account %s: no OHLCV data returned for %s", account.id, account.pair)
                await _record_error(notifier, account, "No market data was returned for this account's pair just now.", now=now)
                return
            signal = generate_signal(
                candles,
                ema_fast_period=settings.ema_fast_period,
                ema_slow_period=settings.ema_slow_period,
                atr_period=settings.atr_period,
                atr_flat_threshold=settings.atr_flat_threshold,
                atr_rolling_window=settings.atr_rolling_window,
            )

        current_price = candles[-1].close
        # A successful evaluation, whatever it concluded, is a healthy
        # cycle - record the signal and clear any previously-recorded
        # error so "Error" status doesn't linger past the problem itself.
        account.last_signal = signal.signal.value
        account.last_signal_at = now
        account.last_error = None
        account.last_error_at = None

        if open_trade is None:
            if scan_pair != account.pair:
                # The only place `pair` is ever reassigned by the bot
                # itself - and only reachable with no open trade (see
                # above). Persisted atomically with the entry that
                # follows, so the dashboard's "current pair" always
                # reflects the pair actually just traded, never an
                # intermediate scan result that didn't lead to a trade.
                account.pair = scan_pair
            await _maybe_enter(session, account, client, signal, current_price, settings, notifier, is_paper=is_paper, now=now)
        else:
            # Only a PAPER trade reaches here: live trades returned above.
            await _maybe_exit(
                session, account, client, open_trade, signal, current_price, notifier, is_paper=effective_is_paper, now=now
            )

        await _maybe_send_daily_summary(session, account, notifier, now=now)
    except exchange.WithdrawalPermissionDetected as exc:
        await _record_error(notifier, account, exchange.safe_error_message(exc), now=now)
        logger.error("account %s: stored key has withdrawal permission, refusing to trade", account.id)
        raise
    except Exception as exc:
        await _record_error(notifier, account, exchange.safe_error_message(exc), now=now)
        raise
    finally:
        await client.close()


async def _notify_safe(notifier: Notifier, account: TradingAccount, message: str) -> None:
    """Best-effort send for the new status notifications below - a failed
    delivery here must never crash or mislabel an otherwise-healthy cycle,
    mirroring the same pattern the Telegram webhook uses for its own replies."""
    try:
        await notifier.send(account.notification_target, message)
    except Exception:
        logger.exception("account %s: failed to send a status notification", account.id)


async def _record_error(notifier: Notifier, account: TradingAccount, message: str, *, now: datetime) -> None:
    """Persists a short, already-sanitized message (see
    exchange.safe_error_message - never raw exception text) and notifies
    once per error *episode*: only when the account wasn't already in an
    error state, so a persistent problem doesn't page the customer every
    cycle until it's fixed."""
    is_new_episode = account.last_error is None
    account.last_error = message
    account.last_error_at = now
    if is_new_episode:
        await _notify_safe(notifier, account, f"MBA Bot hit a problem: {message}")


def _resolve_credentials(account: TradingAccount, settings: Settings, *, is_paper: bool) -> tuple[str | None, str | None]:
    if is_paper:
        # Spot and futures testnet are separate Binance systems with
        # separate key registrations - a key from one is not valid on
        # the other, so which pair to use depends on market_type, not
        # just "is this paper mode".
        if account.market_type == "futures":
            return settings.testnet_futures_api_key, settings.testnet_futures_api_secret
        return settings.testnet_spot_api_key, settings.testnet_spot_api_secret
    credential = account.credential
    if credential is None:
        return None, None
    return (
        decrypt(credential.encrypted_api_key, key=settings.encryption_key),
        decrypt(credential.encrypted_api_secret, key=settings.encryption_key),
    )


async def _scan_auto_pair(client, account: TradingAccount, settings: Settings) -> tuple[str, list, StrategySignal] | None:
    """Auto Select's market scan. Reuses the SAME exchange client
    already built for this account this cycle - same environment, same
    credentials, same account - just queried against each pair in the
    fixed, approved universe (`config.SUPPORTED_PAIRS`, the one list
    this platform already maintains for which pairs it can trade at
    all - not a second, separately-maintained "auto select universe"
    that could silently drift out of sync with it) instead of only the
    account's currently configured pair.

    Ranking itself lives in `pair_selection.rank_candidates` - this
    function's only job is the I/O (fetching candles) and turning the
    winning candidate back into a full `StrategySignal` (needed for its
    `.atr`/`.fast_ema` by the caller's stop-loss and notification code,
    which `pair_selection.RankedPair` deliberately doesn't carry, to
    keep that module's public shape minimal and side-effect free).

    Returns None if nothing in the universe had an actionable signal
    this cycle - never a forced/fallback pick."""
    scanned: list[PairCandidate] = []
    for pair in SUPPORTED_PAIRS:
        try:
            ohlcv = await client.fetch_ohlcv(
                exchange.exchange_symbol(pair, account.market_type), timeframe=settings.timeframe, limit=settings.ohlcv_limit
            )
        except Exception:
            logger.warning("account %s: auto-select scan could not fetch %s this cycle, skipping it", account.id, pair)
            continue
        candles = candles_from_ohlcv(ohlcv)
        if candles:
            scanned.append(PairCandidate(pair=pair, candles=candles))

    ranked = rank_candidates(
        scanned,
        market_type=account.market_type,
        ema_fast_period=settings.ema_fast_period,
        ema_slow_period=settings.ema_slow_period,
        atr_period=settings.atr_period,
        atr_flat_threshold=settings.atr_flat_threshold,
        atr_rolling_window=settings.atr_rolling_window,
    )
    if not ranked:
        return None

    best_pair = ranked[0].pair
    best_candles = next(c.candles for c in scanned if c.pair == best_pair)
    signal = generate_signal(
        best_candles,
        ema_fast_period=settings.ema_fast_period,
        ema_slow_period=settings.ema_slow_period,
        atr_period=settings.atr_period,
        atr_flat_threshold=settings.atr_flat_threshold,
        atr_rolling_window=settings.atr_rolling_window,
    )
    return best_pair, best_candles, signal


async def _maybe_enter(session, account, client, signal, current_price, settings, notifier, *, is_paper, now):
    if signal.signal == SignalType.BULLISH_CROSSOVER:
        side = "long"
    elif signal.signal == SignalType.BEARISH_CROSSOVER and account.market_type == "futures":
        side = "short"
    else:
        return

    stop_price = compute_stop_loss(
        entry_price=current_price, atr=signal.atr, side=side, atr_multiplier=settings.atr_stop_multiplier
    )

    if is_paper:
        # Paper mode keeps its own pre-existing sizing base unchanged -
        # the fixed, synthetic paper_balance IS the sandbox capital, and
        # was never the subject of the "entire exchange balance" finding
        # this allocation model fixes (that only ever applied to the
        # live branch's fetch_available_quote_balance call below).
        effective_capital = account.paper_balance
    else:
        if account.allocated_capital is None:
            # Fail closed: Trading Capital must be an explicit customer
            # choice before any live entry, never a silent "use whatever
            # Binance shows" fallback (see models.py's allocated_capital
            # docstring). resolve_trading_mode's Start Bot gate already
            # keeps an account with no allocation out of LIVE entirely,
            # so this should be unreachable in practice - this check is
            # deliberate defense in depth, not redundancy to be trimmed.
            logger.warning("account %s: live account has no allocated_capital configured, refusing to enter", account.id)
            return
        live_balance = await exchange.fetch_available_quote_balance(client, account.pair)
        # The customer's configured ceiling, never more than what
        # Binance actually shows as free right now - see models.py's
        # allocated_capital docstring and the capital-allocation spec:
        # effective_capital = min(configured_trading_capital, currently_available_balance).
        effective_capital = min(account.allocated_capital, live_balance)
        if effective_capital <= 0:
            return

    size = compute_position_size(
        account_balance=effective_capital,
        risk_pct=account.risk_pct,
        entry_price=current_price,
        stop_price=stop_price,
        max_risk_pct=settings.risk_pct_max,
    )
    if size.quantity <= 0:
        return

    if not is_paper:
        # The risk-% formula alone has no opinion on notional - a tight
        # ATR stop produces a larger quantity for the same dollar risk,
        # unbounded unless something caps quantity directly. This is
        # the conservative, operator-configured backstop (see
        # risk.apply_exposure_ceiling); it's a no-op for Spot at the
        # default multiplier in virtually every real case since Spot
        # can't exceed effective_capital's own notional anyway.
        size = apply_exposure_ceiling(
            size,
            entry_price=current_price,
            effective_capital=effective_capital,
            max_notional_multiplier=settings.max_notional_multiplier,
        )
        venue = build_venue(
            client, market_type=account.market_type, pair=account.pair, spot_stop_limit_buffer_pct=settings.spot_stop_limit_buffer_pct
        )
        ctx = lifecycle.LiveContext(
            session=session, account=account, venue=venue, settings=settings, notifier=notifier, now=now
        )
        try:
            await venue.prepare()
            await lifecycle.preflight_entry(ctx)
        except lifecycle.EntryRefused as exc:
            await _record_error(notifier, account, exc.message, now=now)
            return
        except (exchange.UnsupportedAccountMode, exchange.UnsupportedSymbol) as exc:
            await _record_error(notifier, account, exchange.safe_error_message(exc), now=now)
            return
        except ProtectionUnavailable:
            await _record_error(
                notifier,
                account,
                "An exchange-side protective stop cannot currently be placed for this pair on Binance, so no new "
                "live trade was opened. This will retry automatically on the next check.",
                now=now,
            )
            return

        if account.market_type == "futures":
            # Read-only - this module never calls set_leverage (see
            # exchange.fetch_symbol_leverage). Margin safety for a real
            # Futures order depends on knowing the real leverage: if it
            # can't be reliably read (an exchange error, a missing
            # field, or a non-positive value - fetch_symbol_leverage
            # collapses all three to None, never guessing a value), this
            # is a Mainnet Futures entry whose margin exposure cannot be
            # proven to satisfy the configured limits, so it must fail
            # closed - no order, not "proceed using only the notional
            # ceiling". This never affects Spot (no leverage concept
            # here) and never affects managing an already-open position
            # (see process_account's effective_is_paper/open_trade
            # handling, which this function has no part in) - it is
            # scoped to a brand new live Futures entry only.
            leverage = await exchange.fetch_symbol_leverage(client, venue.symbol, side=side)
            if leverage is None:
                await _record_error(
                    notifier,
                    account,
                    "Could not verify this account's Binance Futures leverage/margin just now, so no new live "
                    "trade was opened. This will retry automatically on the next check.",
                    now=now,
                )
                return
            size = apply_margin_ceiling(
                size, entry_price=current_price, effective_capital=effective_capital, leverage=leverage
            )
        if size.quantity <= 0:
            return
        quantity = venue.plan_entry_quantity(size.quantity, entry_price=current_price, stop_price=stop_price, side=side)
        if quantity <= 0:
            logger.info("account %s: sized quantity cannot satisfy %s exchange filters/protection, no entry", account.id, venue.symbol)
            return
        risk_amount = quantity * size.stop_distance
        await lifecycle.open_live_trade(
            ctx,
            side=side,
            quantity=quantity,
            signal_price=current_price,
            stop_price=stop_price,
            risk_text=f"Risk: {risk_amount:.2f} ({account.risk_pct:.2f}% of balance)",
        )
        return

    trade = Trade(
        account_id=account.id,
        side=side,
        is_paper=is_paper,
        entry_price=current_price,
        quantity=size.quantity,
        stop_loss_price=stop_price,
        status=TRADE_OPEN,
        opened_at=now,
        pair=account.pair,
    )
    session.add(trade)

    mode_label = "PAPER" if is_paper else "LIVE"
    await notifier.send(
        account.notification_target,
        f"[{mode_label}] Entered {side.upper()} {account.pair}\n"
        f"Entry: {current_price:.4f}  Qty: {size.quantity:.6f}  Stop: {stop_price:.4f}\n"
        f"Risk: {size.risk_amount:.2f} ({account.risk_pct:.2f}% of balance)",
    )


async def _maybe_exit(session, account, client, open_trade: Trade, signal, current_price, notifier, *, is_paper, now):
    """PAPER trades only. A live trade's exit is lifecycle.execute_exit /
    lifecycle.monitor_trade - never a bare market order from here."""
    if not is_paper:
        raise RuntimeError("_maybe_exit is paper-only; live exits go through lifecycle.execute_exit")
    hit_stop = (open_trade.side == "long" and current_price <= open_trade.stop_loss_price) or (
        open_trade.side == "short" and current_price >= open_trade.stop_loss_price
    )
    opposite_cross = (open_trade.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (
        open_trade.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER
    )
    if not (hit_stop or opposite_cross):
        return

    exit_price = open_trade.stop_loss_price if hit_stop else current_price

    if open_trade.side == "long":
        pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
    else:
        pnl = (open_trade.entry_price - exit_price) * open_trade.quantity

    open_trade.exit_price = exit_price
    open_trade.pnl = pnl
    open_trade.status = "closed"
    open_trade.closed_at = now
    open_trade.close_reason = "stop_loss" if hit_stop else "crossover_exit"

    account.paper_balance += pnl

    mode_label = "PAPER"
    await notifier.send(
        account.notification_target,
        f"[{mode_label}] Exited {open_trade.side.upper()} {account.pair} ({open_trade.close_reason})\n"
        f"Entry: {open_trade.entry_price:.4f}  Exit: {exit_price:.4f}  P&L: {pnl:+.2f}",
    )


async def _manage_live_trade(session, account, client, trade: Trade, settings, notifier, *, now):
    """Verify the exchange can be managed safely, then reconcile `trade`
    against Binance. Returns (trade, LiveContext)."""
    venue = build_venue(
        client,
        market_type=account.market_type,
        pair=trade.pair or account.pair,
        spot_stop_limit_buffer_pct=settings.spot_stop_limit_buffer_pct,
    )
    ctx = lifecycle.LiveContext(session=session, account=account, venue=venue, settings=settings, notifier=notifier, now=now)
    try:
        await venue.prepare(managing=True)
    except (exchange.UnsupportedAccountMode, exchange.UnsupportedSymbol, ProtectionUnavailable):
        # The position exists but the exchange configuration is one we
        # cannot act on safely: stop, surface it, never guess.
        await lifecycle.mark_attention(ctx, trade, lifecycle.ATTN_CANNOT_VERIFY)
        return trade, ctx
    trade = await lifecycle.reconcile_trade(ctx, trade)
    return trade, ctx


async def reconcile_account(
    session: Session,
    account: TradingAccount,
    settings: Settings,
    notifier: Notifier,
    *,
    current_price: float | None = None,
) -> None:
    """Reconcile this account's live trade (if any) against Binance and do
    nothing else - no scan, no entry. Used at startup and by the fast
    recovery loop (scheduler.py); the main loop does the same inside
    process_account. With `current_price` (the fast stop-limit watch) the
    protection-health check also runs: if the price has gone through a
    resting stop-limit's executable limit, the lifecycle cancels it,
    confirms, and market-closes the remainder."""
    trade = get_open_trade(session, account.id)
    if trade is None or trade.is_paper:
        return
    now = datetime.now(timezone.utc)
    api_key, api_secret = _resolve_credentials(account, settings, is_paper=False)
    if api_key is None:
        logger.warning("account %s: live trade but no usable credentials, cannot reconcile", account.id)
        return
    client = exchange.build_exchange_client(
        exchange="binance", market_type=account.market_type, api_key=api_key, api_secret=api_secret, sandbox=False
    )
    try:
        await exchange.assert_trade_only(client)
        trade, ctx = await _manage_live_trade(session, account, client, trade, settings, notifier, now=now)
        if current_price is not None:
            await lifecycle.monitor_trade(ctx, trade, crossover_exit=False, current_price=current_price)
    finally:
        await client.close()


async def _maybe_send_daily_summary(session, account, notifier, *, now) -> None:
    today = now.strftime("%Y-%m-%d")
    if account.last_pnl_summary_date == today:
        return

    closed_today = get_trades_closed_on(session, account.id, today)
    account.last_pnl_summary_date = today
    if not closed_today:
        return

    total_pnl = sum(t.pnl or 0.0 for t in closed_today)
    quote = exchange.quote_currency(account.pair)
    await notifier.send(
        account.notification_target,
        f"Daily P&L summary ({today} UTC): {len(closed_today)} trade(s) closed, net {total_pnl:+.2f} {quote}",
    )
