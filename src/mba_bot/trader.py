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

from . import exchange
from .config import Settings
from .crypto_utils import decrypt
from .db import get_open_trade, get_trades_closed_on
from .models import Trade, TradingAccount
from .notifications import Notifier
from .onboarding import TradingMode, resolve_trading_mode
from .risk import compute_position_size, compute_stop_loss
from .strategy import SignalType, candles_from_ohlcv, generate_signal

logger = logging.getLogger(__name__)


async def process_account(session: Session, account: TradingAccount, settings: Settings, notifier: Notifier) -> None:
    if account.notification_target is None:
        logger.info("account %s: notification channel not linked yet, skipping until it is", account.id)
        return

    now = datetime.now(timezone.utc)
    trial_state = resolve_trading_mode(
        trial_ends_at=account.trial_ends_at, live_bypass_accepted_at=account.live_bypass_accepted_at, now=now
    )
    is_paper = trial_state.mode == TradingMode.PAPER

    api_key, api_secret = _resolve_credentials(account, settings, is_paper=is_paper)
    if api_key is None:
        logger.warning("account %s has no usable credentials for mode=%s, skipping", account.id, trial_state.mode.value)
        return

    client = exchange.build_exchange_client(
        exchange="binance",
        market_type=account.market_type,
        api_key=api_key,
        api_secret=api_secret,
        sandbox=is_paper,
    )
    try:
        if not is_paper:
            await exchange.assert_trade_only(client)

        ohlcv = await client.fetch_ohlcv(account.pair, timeframe=settings.timeframe, limit=settings.ohlcv_limit)
        candles = candles_from_ohlcv(ohlcv)
        if not candles:
            logger.warning("account %s: no OHLCV data returned for %s", account.id, account.pair)
            return
        current_price = candles[-1].close

        signal = generate_signal(
            candles,
            ema_fast_period=settings.ema_fast_period,
            ema_slow_period=settings.ema_slow_period,
            atr_period=settings.atr_period,
            atr_flat_threshold=settings.atr_flat_threshold,
            atr_rolling_window=settings.atr_rolling_window,
        )

        open_trade = get_open_trade(session, account.id)
        if open_trade is None:
            await _maybe_enter(session, account, client, signal, current_price, settings, notifier, is_paper=is_paper, now=now)
        else:
            await _maybe_exit(session, account, client, open_trade, signal, current_price, notifier, is_paper=is_paper, now=now)

        await _maybe_send_daily_summary(session, account, notifier, now=now)
    except exchange.WithdrawalPermissionDetected:
        logger.error("account %s: stored key has withdrawal permission, refusing to trade", account.id)
        raise
    finally:
        await client.close()


def _resolve_credentials(account: TradingAccount, settings: Settings, *, is_paper: bool) -> tuple[str | None, str | None]:
    if is_paper:
        return settings.testnet_api_key, settings.testnet_api_secret
    credential = account.credential
    if credential is None:
        return None, None
    return (
        decrypt(credential.encrypted_api_key, key=settings.encryption_key),
        decrypt(credential.encrypted_api_secret, key=settings.encryption_key),
    )


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
    balance = account.paper_balance if is_paper else await exchange.fetch_available_quote_balance(client, account.pair)
    size = compute_position_size(
        account_balance=balance,
        risk_pct=account.risk_pct,
        entry_price=current_price,
        stop_price=stop_price,
        max_risk_pct=settings.risk_pct_max,
    )
    if size.quantity <= 0:
        return

    if not is_paper:
        order_side = "buy" if side == "long" else "sell"
        await client.create_market_order(account.pair, order_side, size.quantity)

    trade = Trade(
        account_id=account.id,
        side=side,
        is_paper=is_paper,
        entry_price=current_price,
        quantity=size.quantity,
        stop_loss_price=stop_price,
        status="open",
        opened_at=now,
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
    hit_stop = (open_trade.side == "long" and current_price <= open_trade.stop_loss_price) or (
        open_trade.side == "short" and current_price >= open_trade.stop_loss_price
    )
    opposite_cross = (open_trade.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (
        open_trade.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER
    )
    if not (hit_stop or opposite_cross):
        return

    exit_price = open_trade.stop_loss_price if hit_stop else current_price

    if not is_paper:
        order_side = "sell" if open_trade.side == "long" else "buy"
        await client.create_market_order(account.pair, order_side, open_trade.quantity)

    if open_trade.side == "long":
        pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
    else:
        pnl = (open_trade.entry_price - exit_price) * open_trade.quantity

    open_trade.exit_price = exit_price
    open_trade.pnl = pnl
    open_trade.status = "closed"
    open_trade.closed_at = now
    open_trade.close_reason = "stop_loss" if hit_stop else "crossover_exit"

    if is_paper:
        account.paper_balance += pnl

    mode_label = "PAPER" if is_paper else "LIVE"
    await notifier.send(
        account.notification_target,
        f"[{mode_label}] Exited {open_trade.side.upper()} {account.pair} ({open_trade.close_reason})\n"
        f"Entry: {open_trade.entry_price:.4f}  Exit: {exit_price:.4f}  P&L: {pnl:+.2f}",
    )


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
