"""One evaluation cycle for one customer account.

`process_account` is the unit of work the scheduler fans out over every
active account each poll interval. It is written to fail closed and
isolated: any exception here is caught by the caller (scheduler.py) so one
customer's bad data or exchange error never stops another customer's loop.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import account_loss, daily_limit as daily_limit_module, demo as demo_module, small_capital, donchian, exchange, foreign_orders, kill_switch, leverage as leverage_module, risk_limits as risk_limits_module, lifecycle, multi_trade, pair_catalog, rsi_mr, pnl_labels, protection_guard, strategy_guard_rules, strategy_guards, telemetry
from . import risk as risk_module
from .billing.entitlement import is_entitled
from .config import Settings, auto_pairs, manual_pairs
from .crypto_utils import decrypt
from .db import get_open_trade, get_trades_closed_on, list_open_trades
from .indicators import atr as atr_series
from .models import PROTECTION_PROTECTED, TRADE_OPEN, Trade, TradingAccount
from .notifications import Notifier
from .onboarding import TradingMode, resolve_trading_mode
from .pair_selection import PairCandidate, rank_candidates
from .risk import (
    apply_exposure_ceiling,
    apply_margin_ceiling,
    assess_stop_viability,
    compute_position_size,
    compute_stop_loss,
    StopViability,
)
from .strategy import SignalType, StrategySignal, candles_from_ohlcv, generate_signal
from .venue import ProtectionUnavailable, build_venue, rules_from_market

logger = logging.getLogger(__name__)


def _ema_signal_candles(candles: list, settings: Settings, now_ms: int) -> list:
    """The candles the EMA strategy decides on (docs/safety/ema_closed_candle.md). Switch off: the very list it was given, so behaviour is
    exactly what it always was. Switch on: only candles that have closed at `now_ms`; Binance returns the still-forming candle as the last row.
    An unsupported timeframe returns no candles (fail closed: no signal, no entry, no crossover exit)."""
    if not settings.ema_closed_candle_only:
        return candles
    tf, supported = rsi_mr.normalize_timeframe(settings.timeframe)
    if not supported:
        if protection_guard.should_alert("-", "-", f"ema_timeframe:{settings.timeframe}", now=datetime.now(timezone.utc), repeat_hours=24.0):
            logger.warning("EMA_CLOSED_CANDLE_ONLY is on but TIMEFRAME %r is not a supported size; EMA takes no new signal until it is fixed", settings.timeframe)
        return []
    return rsi_mr.closed_only(candles, now_ms, rsi_mr.timeframe_ms(tf))


def _ema_generate(candles: list, settings: Settings) -> StrategySignal:
    return generate_signal(
        candles,
        ema_fast_period=settings.ema_fast_period,
        ema_slow_period=settings.ema_slow_period,
        atr_period=settings.atr_period,
        atr_flat_threshold=settings.atr_flat_threshold,
        atr_rolling_window=settings.atr_rolling_window,
    )


def _ema_entry_stale(session, settings: Settings, account, signal, signal_candles: list, pair: str, *, now: datetime, now_ms: int, is_paper: bool) -> bool:
    """True if an EMA entry must be skipped as late: the signal candle closed more than EMA_ENTRY_WINDOW_MINUTES ago (a crossover on the last
    closed candle stays visible for the whole next candle). Always False with the switch off, and for a signal that would not be entered anyway
    (no crossover, or a bearish one on Spot). Logged once per signal candle."""
    if not settings.ema_closed_candle_only:
        return False
    if signal.signal != SignalType.BULLISH_CROSSOVER and not (signal.signal == SignalType.BEARISH_CROSSOVER and account.market_type == "futures"):
        return False
    tf, _ = rsi_mr.normalize_timeframe(settings.timeframe)
    tf_ms = rsi_mr.timeframe_ms(tf)
    if not signal_candles:
        return True
    last_ts = signal_candles[-1].timestamp
    age_ms = now_ms - (last_ts + tf_ms)
    if 0 <= age_ms <= int(settings.ema_entry_window_minutes * 60_000):
        return False
    if protection_guard.should_alert(account.id, pair, f"stale:ema:{last_ts}", now=now, repeat_hours=24.0):
        logger.info("account %s: EMA signal on %s skipped (signal_stale): the signal candle closed %.0f minutes ago, the window is %g",
                    account.id, pair, age_ms / 60_000, settings.ema_entry_window_minutes)
        telemetry.log_decision(session, settings, account, stage=telemetry.STAGE_ENTRY, outcome=telemetry.SKIPPED, reason="signal_stale",
                               is_paper=is_paper, now=now, pair=pair, strategy_id=multi_trade.EMA_STRATEGY_ID, candle_ts=last_ts,
                               detail={"age_minutes": round(age_ms / 60_000, 1), "window_minutes": settings.ema_entry_window_minutes})
    return True


async def process_account(session: Session, account: TradingAccount, settings: Settings, notifier: Notifier) -> None:
    if account.notification_target is None:
        logger.info("account %s: notification channel not linked yet, skipping until it is", account.id)
        return

    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)  # one clock reading per cycle: every EMA call site below drops the forming candle against it
    # We attempted a real cycle for this account - record that regardless
    # of how the rest of this function goes, so "last checked" on the
    # dashboard reflects reality even during a run of errors.
    account.last_scanned_at = now
    _cycle_equity.pop(account.id, None)
    # Persist the heartbeat NOW, in its own short synchronous transaction, so a
    # cycle that later stalls, errors or fails to save still leaves reliable
    # evidence that the scheduler reached this account. (Scheduler sessions do
    # not flush implicitly - see db.session_scope - so this is also the only
    # place the stamp is written before the cycle's I/O begins.)
    session.commit()

    trial_state = resolve_trading_mode(
        trial_ends_at=account.trial_ends_at,
        live_bypass_accepted_at=account.live_bypass_accepted_at,
        has_paid=is_entitled(account.customer, settings=settings, now=now),
        live_trading_enabled_at=account.live_trading_enabled_at,
        has_mainnet_credential=account.has_mainnet_credential,
        bot_started_at=account.bot_started_at,
        now=now,
        demo_enabled=demo_module.available(account, settings),
    )
    is_paper = trial_state.mode == TradingMode.PAPER
    is_demo = trial_state.mode == TradingMode.DEMO  # real orders on the demo system with the customer's own demo keys; never real money

    if not is_paper:
        # Visibility only (a forgotten kill switch must not stay on silently): one journal line per repeat window while
        # engaged, and one when a read needed retries. The entry paths below read the switch again themselves.
        ks = await kill_switch.acheck(session, settings)
        if ks.engaged or ks.attempts > 1:
            kill_switch.log_decision(ks, scope=account.id, now=now, repeat_hours=settings.protection_alert_repeat_hours)

    if not is_paper and not is_demo and account.live_notified_at is None:
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
    open_trades = list_open_trades(session, account.id)
    open_trade = open_trades[0] if open_trades else None
    # With more than one open trade (a paper multi-trade account, or an unexpected live pair) the client is a live one if ANY is live.
    effective_is_paper = all(t.is_paper for t in open_trades) if open_trades else is_paper
    effective_is_demo = any(t.is_demo for t in open_trades) if open_trades else is_demo

    api_key, api_secret = _resolve_credentials(account, settings, is_paper=effective_is_paper, is_demo=effective_is_demo)
    if api_key is None:
        logger.warning(
            "account %s has no usable credentials for mode=%s, skipping",
            account.id,
            "demo" if effective_is_demo else "paper" if effective_is_paper else "live",
        )
        await _record_error(notifier, account, "No usable trading credentials are configured for this account.", now=now)
        return

    client = exchange.build_exchange_client(
        exchange="binance",
        market_type=account.market_type,
        api_key=api_key,
        api_secret=api_secret,
        sandbox=effective_is_paper or effective_is_demo,  # a demo client is proven (exchange.assert_routing) to route only to the demo system
    )
    try:
        # Withdrawal permission is checked every cycle for a live key, but it
        # means two different things (see exchange.has_withdrawal_permission):
        # it ends NEW entries for good (durable block until the credential is
        # replaced), while an ALREADY-OPEN position keeps being managed, for
        # risk reduction only - that path runs on a venue that cannot place
        # an entry at all.
        withdrawal_detected = False
        if not effective_is_paper and not effective_is_demo:
            withdrawal_detected = await exchange.has_withdrawal_permission(client)
            if withdrawal_detected:
                await _flag_withdrawal_block(session, account, notifier, now=now)

        if _use_multi(account, settings, is_paper, open_trades):
            # More than one trade, or a PAPER multi-trade account (MAX_OPEN_POSITIONS above 1 and an owner-set pair list). With
            # the defaults (one trade, one pair) this is never true and the single-trade path below runs exactly as before.
            await _process_multi(session, account, client, open_trades, settings, notifier, is_paper=is_paper, now=now)
            return

        if open_trade is not None and not open_trade.is_paper:
            # A live position is reconciled against Binance FIRST, before
            # any market data is needed: restart recovery, a stop that
            # fired while we were offline, an unprotected or unresolved
            # state all get handled here, whatever the strategy says.
            open_trade, ctx = await _manage_live_trade(session, account, client, open_trade, settings, notifier, now=now, is_demo=effective_is_demo)
            account.last_error = None
            account.last_error_at = None
            if open_trade.status != TRADE_OPEN or open_trade.protection_status != PROTECTION_PROTECTED:
                # Closed this cycle (back to scanning next cycle), or not
                # healthy and being worked by the reconciler: no strategy
                # decisions on a trade whose protection is not verified.
                await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)
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
            sig_candles = _ema_signal_candles(candles, settings, now_ms)  # price below stays from the raw last row; only the signal waits for a close
            signal = _ema_generate(sig_candles, settings)
            account.last_signal = signal.signal.value
            account.last_signal_at = now
            opposite_cross = (open_trade.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (
                open_trade.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER
            )
            await lifecycle.monitor_trade(ctx, open_trade, crossover_exit=opposite_cross, current_price=candles[-1].close)
            await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)
            return

        if open_trade is None and not is_paper and (withdrawal_detected or account.withdrawal_blocked_at is not None):
            # No position and a withdrawal-capable (or still-blocked) credential:
            # no scan, no entry. Nothing to manage; the customer was alerted once.
            account.last_error = None
            account.last_error_at = None
            return

        # Auto Select only ever runs this scan when there is no open
        # trade (the `open_trade is None` check right here) - that, not
        # just the API-layer guard in api/routers/account.py, is what
        # makes "never switch pairs while a position is open" true in
        # the backend trading logic itself: there is no code path from
        # this branch into an account that has one.
        scan_info = None
        if open_trade is None and account.pair_selection_mode == "auto":
            # Capital-based skip only for a LIVE Futures account with the pair
            # expansion on; otherwise None = the scan is exactly what it always was.
            auto_capital = (
                account.allocated_capital
                if (account.market_type == "futures" and settings.futures_pair_expansion and not is_paper)
                else None
            )
            viability_out: list | None = None
            if settings.small_capital_pairs and not is_paper and account.allocated_capital is not None:
                auto_capital, viability_out = account.allocated_capital, []  # every live account, Spot included, scans only pairs its capital can trade
            scan_out: dict | None = {} if telemetry.enabled(settings) else None
            auto_result = await _scan_auto_pair(client, account, settings, capital=auto_capital, scan_out=scan_out, now_ms=now_ms, viability_out=viability_out)
            for skipped_pair, v in viability_out or []:
                try:
                    small_capital.record_skip(session, account, skipped_pair, small_capital.R_BELOW_MIN_CAPITAL, v, now=now)
                except Exception:  # noqa: BLE001
                    logger.warning("account %s: could not record the pair skip", account.id)
            if scan_out is not None:
                scan_info = _scan_info(scan_out, auto_result)
                telemetry.log_auto_scan(
                    session, settings, account, scan_info, _scan_events(scan_out), is_paper=is_paper, now=now
                )
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
            sig_candles = _ema_signal_candles(candles, settings, now_ms)
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
            sig_candles = _ema_signal_candles(candles, settings, now_ms)
            signal = _ema_generate(sig_candles, settings)

        current_price = candles[-1].close
        # A successful evaluation, whatever it concluded, is a healthy
        # cycle - record the signal and clear any previously-recorded
        # error so "Error" status doesn't linger past the problem itself.
        account.last_signal = signal.signal.value
        account.last_signal_at = now
        account.last_error = None
        account.last_error_at = None

        tctx = None
        if open_trade is None and telemetry.enabled(settings) and (
            signal.signal != SignalType.NONE or "flat" in (signal.reason or "")
        ):
            # Research telemetry only (never read by the trading path): what the
            # strategy concluded, and the market features at that moment.
            tctx = telemetry.build_entry_context(sig_candles, signal, settings, now=now, scan=scan_info)
            telemetry.signal_stage(session, settings, account, tctx, signal, is_paper=is_paper, now=now)

        if open_trade is None:
            if scan_pair != account.pair:
                # The only place `pair` is ever reassigned by the bot
                # itself - and only reachable with no open trade (see
                # above). Persisted atomically with the entry that
                # follows, so the dashboard's "current pair" always
                # reflects the pair actually just traded, never an
                # intermediate scan result that didn't lead to a trade.
                account.pair = scan_pair
            if _ema_entry_stale(session, settings, account, signal, sig_candles, scan_pair, now=now, now_ms=now_ms, is_paper=is_paper):
                pass  # a closed-candle crossover that is too old to enter at a fair price; logged once per candle
            else:
                await _maybe_enter(
                    session, account, client, signal, current_price, settings, notifier, is_paper=is_paper, now=now, tctx=tctx, is_demo=is_demo
                )
        else:
            # Only a PAPER trade reaches here: live trades returned above.
            await _maybe_exit(
                session, account, client, open_trade, signal, current_price, notifier, is_paper=effective_is_paper, now=now,
                settings=settings,
            )

        await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)
    except exchange.WithdrawalPermissionDetected as exc:
        await _record_error(notifier, account, exchange.safe_error_message(exc), now=now)
        logger.error("account %s: stored key has withdrawal permission, refusing to trade", account.id)
        raise
    except Exception as exc:
        await _record_error(notifier, account, exchange.safe_error_message(exc), now=now)
        raise
    finally:
        try:
            # The equity sample and the daily loss pause run on EVERY normal end of a live cycle, whichever `return` it took. Not after an exception
            # (the exchange is probably unreachable and the cycle's own error handling has already run).
            if not effective_is_paper and not effective_is_demo and sys.exc_info()[0] is None:  # the equity samples and loss limits are the live account's; demo money must not mix into them
                await _account_equity_tick(session, account, client, settings, notifier, now=now)
                await _foreign_order_check(session, account, client, settings, notifier, now=now)
        finally:
            _cycle_equity.pop(account.id, None)
            await client.close()


def _leverage_mode(settings: Settings, now: datetime) -> str:
    """strict or size_cap for this entry. size_cap only takes effect with PROTECTION_PRECHECK_MODE=enforce: without the liquidation check it must not run, so it
    behaves as strict and says so once an hour (never silently size_cap without the check)."""
    if settings.futures_leverage_mode != leverage_module.MODE_SIZE_CAP:
        return leverage_module.MODE_STRICT
    if settings.protection_precheck_mode != "enforce":
        if protection_guard.should_alert("-", "-", "leverage_mode_fallback", now=now, repeat_hours=1.0):
            logger.warning("FUTURES_LEVERAGE_MODE=size_cap needs PROTECTION_PRECHECK_MODE=enforce (it is %s); behaving as strict", settings.protection_precheck_mode)
        return leverage_module.MODE_STRICT
    return leverage_module.MODE_SIZE_CAP


# One equity reading per account per cycle: the entry gate and the end-of-cycle tick share it (key: account id; cleared when a cycle starts and when it ends).
_cycle_equity: dict[str, tuple[datetime, float | None, str]] = {}

# Trade states in which a Spot position holds coins the bot must count at mark. Any other non-terminal state (pending, attention) cannot be valued: the
# reading is then unreadable rather than guessed.
_SPOT_HELD_STATES = (TRADE_OPEN, "protecting", "closing")


async def _live_equity(session, account: TradingAccount, client) -> tuple[float | None, str]:
    """(equity, reason) for a LIVE account right now: one balance read, plus one ticker read per open Spot position. Never raises."""
    try:
        positions: list[tuple[float, float]] | None = None
        if account.market_type == "spot":
            session.flush()  # the cycle's own changes (a trade closed a moment ago) must be visible to this query
            held = [t for t in list_open_trades(session, account.id) if not t.is_paper]
            if any(t.status not in _SPOT_HELD_STATES for t in held):
                return None, "position_state_unknown"
            positions = []
            for t in held:
                ticker = await client.fetch_ticker(exchange.exchange_symbol(t.pair or account.pair, "spot"))
                mark = exchange._finite_number(ticker.get("last")) if isinstance(ticker, dict) else None
                if mark is None:
                    return None, exchange.EQUITY_MARK_INVALID
                positions.append((t.quantity, mark))
        return await exchange.fetch_account_equity_ex(client, account.pair, account.market_type, positions)
    except Exception:  # noqa: BLE001
        return None, exchange.EQUITY_CALL_FAILED


@dataclass
class _LossCheck:
    state: str  # off | ok | paused | paused_new | unreadable | bad_timezone | no_limit
    reason: str = ""
    pause: object | None = None
    approximate: bool = False


async def _cycle_equity_read(session, account: TradingAccount, client, now: datetime) -> tuple[float | None, str]:
    """The cycle's one equity reading (cached for the cycle; the cache is cleared at the start and the end of every cycle)."""
    cached = _cycle_equity.get(account.id)
    if cached is not None:
        return cached[1], cached[2]
    equity, reason = await _live_equity(session, account, client)
    _cycle_equity[account.id] = (now, equity, reason)
    return equity, reason


@dataclass
class _TotalCheck:
    state: str  # off | ok | stopped | stopped_new | unreadable
    reason: str = ""
    stop: object | None = None


async def _account_total_stop_check(session, account: TradingAccount, client, settings: Settings, notifier: Notifier, *, now: datetime) -> _TotalCheck:
    """The total loss stop (ACCOUNT_TOTAL_LOSS_STOP, docs/safety/risk_per_trade.md) for one LIVE account: new entries stop when equity is 50 percent or more (risk_limits.TOTAL_STOP_PCT)
    below its reference equity, until the customer resets it. Shares the cycle's one equity reading. Never touches a trade. Fail closed: an unreadable equity
    is reported, not assumed fine. The reference is the first healthy reading after the stop is switched on or after the customer's last reset."""
    if not settings.account_total_loss_stop:
        return _TotalCheck("off")
    existing = account_loss.active_total_stop(session, account.id)
    if existing is not None:
        return _TotalCheck("stopped", "", existing)
    equity, reason = await _cycle_equity_read(session, account, client, now)
    if equity is None:
        return _TotalCheck("unreadable", reason)
    ref = account.loss_reference_equity
    if ref is None:
        account.loss_reference_equity = equity
        account.loss_reference_at = now
        return _TotalCheck("ok")
    loss = risk_limits_module.total_loss_hit(reference_equity=ref, equity=equity)
    if loss is None:
        return _TotalCheck("ok")
    threshold = risk_limits_module.total_loss_threshold()
    stop = account_loss.write_total_stop(session, account.id, reference_equity=ref, equity=equity, loss_pct=loss, threshold_pct=threshold, risk_pct=account.risk_pct, now=now)
    logger.warning("account %s: total loss stop: down %.2f%% from the reference equity, limit %.2f%%; no new entries until the customer resets it", account.id, stop.loss_pct, threshold)
    account.last_error = account_loss.CUSTOMER_MESSAGE_TOTAL_STOP
    account.last_error_at = now
    await _notify_safe(notifier, account, f"MBA Bot: {account_loss.CUSTOMER_MESSAGE_TOTAL_STOP} (down {stop.loss_pct:.1f}%, limit {threshold:g}%)")
    return _TotalCheck("stopped_new", "", stop)


async def _account_loss_check(session, account: TradingAccount, client, settings: Settings, notifier: Notifier, *, now: datetime) -> _LossCheck:
    """The whole daily-loss decision for one LIVE account, shared by the end-of-cycle tick and the entry gate (docs/safety/account_daily_loss.md).

    Reads equity once per cycle (cached for the cycle), records the sample, and writes the pause (audit row plus one alert) the first time the limit is crossed.
    It never touches a trade. Fail closed: an invalid zone or an unreadable equity is reported, not assumed fine."""
    if not settings.account_daily_loss_limit:
        return _LossCheck("off")
    tz = account_loss.zone(settings.account_day_timezone)
    if tz is None:
        return _LossCheck("bad_timezone")
    daily_limit_module.apply_pending(account, account_loss.day_key(now, tz))  # a raise the customer made earlier starts only now, at the first check of its day
    limit = account.daily_loss_limit_usd
    if not (isinstance(limit, (int, float)) and limit > 0):
        return _LossCheck("no_limit")  # a live account with no amount takes no new entry (fail closed)
    existing = account_loss.active_pause(session, account.id, now, tz)
    equity, reason = await _cycle_equity_read(session, account, client, now)
    if equity is None:
        return _LossCheck("paused", reason, existing) if existing is not None else _LossCheck("unreadable", reason)
    row = account_loss.record_sample(session, account.id, equity, now, tz)  # keeps sampling during a pause so the next day can start from it
    if existing is not None:
        return _LossCheck("paused", "", existing, bool(row.approximate))
    hit = account_loss.daily_loss_hit(start_equity=row.start_equity, equity=equity, limit_usd=limit)
    if hit is None:
        return _LossCheck("ok", "", None, bool(row.approximate))
    pause = account_loss.write_pause(session, account.id, hit, limit_usd=limit, now=now, tz=tz, tz_name=settings.account_day_timezone)
    logger.warning("account %s: daily loss pause (%s) loss %.2f USDT limit %g USDT; no new entries until %s", account.id, pause.day_key, hit.loss_usd, limit, pause.ends_at.isoformat())
    account.last_error = account_loss.CUSTOMER_MESSAGE_PAUSE
    account.last_error_at = now
    await _notify_safe(notifier, account, account_loss.pause_message(loss_usd=hit.loss_usd, reference_equity=account.loss_reference_equity, equity=equity))
    return _LossCheck("paused_new", "", pause, bool(row.approximate))


async def _account_equity_tick(session, account: TradingAccount, client, settings: Settings, notifier: Notifier, *, now: datetime) -> None:
    """Runs once at the end of every cycle of a LIVE account that has a client, whatever path the cycle took (idle scan, withdrawal block, no market data, multi
    path, open-trade path): records the equity sample and writes the daily loss pause the moment the limit is crossed. It never raises, never places or cancels an
    order and never touches a trade. A failed read logs one warning (once an hour per account) and the cycle carries on."""
    if not settings.account_daily_loss_limit and not settings.account_total_loss_stop:
        return
    try:
        check = await _account_loss_check(session, account, client, settings, notifier, now=now)
        total = await _account_total_stop_check(session, account, client, settings, notifier, now=now)
        if total.state == "stopped_new":
            stop = total.stop
            telemetry.log_decision(
                session, settings, account, stage=telemetry.STAGE_ENTRY, outcome=telemetry.REJECTED, reason=account_loss.R_ACCOUNT_TOTAL_STOP, is_paper=False, now=now,
                detail=dict(loss_pct=round(stop.loss_pct, 3), threshold_pct=stop.threshold_pct, source="cycle_tick"),
            )
        elif total.state == "unreadable" and check.state == "off":
            if protection_guard.should_alert(account.id, "-", "equity_sample_failed", now=now, repeat_hours=1.0):
                logger.warning("account %s: could not record an equity sample this cycle (%s)", account.id, total.reason)
        if check.state == "paused_new":
            pause = check.pause
            telemetry.log_decision(
                session, settings, account, stage=telemetry.STAGE_ENTRY, outcome=telemetry.REJECTED, reason=account_loss.R_ACCOUNT_DAILY_PAUSE, is_paper=False, now=now,
                detail=dict(loss_pct=round(pause.loss_pct, 3), limit_usd=pause.limit_usd, day=pause.day_key, source="cycle_tick", approximate_start=check.approximate),
            )
        elif check.state in ("unreadable", "bad_timezone", "no_limit") or (check.state == "paused" and check.reason):
            if protection_guard.should_alert(account.id, "-", "equity_sample_failed", now=now, repeat_hours=1.0):
                logger.warning("account %s: could not record an equity sample this cycle (%s)", account.id, check.reason or check.state)
    except Exception:  # noqa: BLE001
        logger.warning("account %s: could not record an equity sample this cycle", account.id)


async def _account_loss_blocks(session, account: TradingAccount, client, settings: Settings, notifier: Notifier, tlog, *, now: datetime) -> bool:
    """True when a NEW live entry must not be opened because of the account-wide daily loss pause (docs/safety/account_daily_loss.md). Fail closed: an invalid time
    zone or an unreadable equity also blocks. Off (ACCOUNT_DAILY_LOSS_LIMIT unset) never blocks and reads nothing. Open trades are not touched by any of this."""
    check = await _account_loss_check(session, account, client, settings, notifier, now=now)
    if check.state in ("off", "ok"):
        total = await _account_total_stop_check(session, account, client, settings, notifier, now=now)
        if total.state in ("stopped", "stopped_new"):
            stop = total.stop
            tlog(telemetry.REJECTED, account_loss.R_ACCOUNT_TOTAL_STOP, loss_pct=round(stop.loss_pct, 3), threshold_pct=stop.threshold_pct)
            account.last_error = account_loss.CUSTOMER_MESSAGE_TOTAL_STOP
            account.last_error_at = now
            return True
        if total.state == "unreadable":
            tlog(telemetry.REJECTED, account_loss.R_EQUITY_UNREADABLE, why=total.reason)
            await _block_entry(notifier, account, settings, account_loss.R_EQUITY_UNREADABLE, account_loss.CUSTOMER_MESSAGE_UNREADABLE, now=now)
            return True
        return False
    if check.state == "no_limit":
        tlog(telemetry.REJECTED, account_loss.R_LIMIT_MISSING)
        await _block_entry(notifier, account, settings, account_loss.R_LIMIT_MISSING, account_loss.CUSTOMER_MESSAGE_LIMIT_MISSING, now=now)
        return True
    if check.state == "bad_timezone":
        tlog(telemetry.REJECTED, account_loss.R_TIMEZONE_INVALID)
        await _block_entry(notifier, account, settings, account_loss.R_TIMEZONE_INVALID, account_loss.CUSTOMER_MESSAGE_TIMEZONE, now=now)
        return True
    if check.state == "unreadable":
        tlog(telemetry.REJECTED, account_loss.R_EQUITY_UNREADABLE, why=check.reason)
        await _block_entry(notifier, account, settings, account_loss.R_EQUITY_UNREADABLE, account_loss.CUSTOMER_MESSAGE_UNREADABLE, now=now)
        return True
    pause = check.pause
    tlog(telemetry.REJECTED, account_loss.R_ACCOUNT_DAILY_PAUSE, loss_pct=round(pause.loss_pct, 3), limit_usd=pause.limit_usd, day=pause.day_key,
         approximate_start=check.approximate, ends_at=pause.ends_at.isoformat())
    account.last_error = account_loss.CUSTOMER_MESSAGE_PAUSE
    account.last_error_at = now
    return True


_foreign_last: dict[str, float] = {}  # account id -> monotonic time of the last foreign-order look (this process only)


def _foreign_now() -> float:
    import time as _time

    return _time.monotonic()


async def _foreign_order_check(session, account: TradingAccount, client, settings: Settings, notifier: Notifier, *, now: datetime) -> None:
    """The foreign-order alarm (docs/safety/foreign_orders.md) for one LIVE account at the end of a normal cycle. Off by default. Read-only: it only reads the account's open orders and positions, tells the
    customer once per distinct set of findings per 24 hours, and never cancels, closes or changes anything. At most one look per account every FOREIGN_ORDER_CHECK_MINUTES. Never raises."""
    if settings.foreign_order_alarm != foreign_orders.MODE_ALERT:
        return
    try:
        started = _foreign_now()
        last = _foreign_last.get(account.id)
        if last is not None and started - last < settings.foreign_order_check_minutes * 60.0:
            return
        _foreign_last[account.id] = started
        session.flush()
        trades = [t for t in list_open_trades(session, account.id) if not t.is_paper]
        pairs = {t.pair or account.pair for t in trades}
        universe = {account.pair, *pairs, *manual_pairs(account.market_type, futures_expansion=settings.futures_pair_expansion)}
        symbols = sorted(exchange.exchange_symbol(p, account.market_type) for p in universe)
        orders, positions, failed = await foreign_orders.read_account_state(client, account.market_type, symbols)
        findings = foreign_orders.find_foreign(orders, positions, pairs)
        if failed and not findings:
            if protection_guard.should_alert(account.id, "-", "foreign_read_failed", now=now, repeat_hours=1.0):
                logger.warning("account %s: foreign-order check could not read %d part(s) of the account this time", account.id, failed)
            return
        if not findings:
            return
        fp = foreign_orders.fingerprint(findings)
        if protection_guard.should_alert(account.id, "-", f"foreign:{fp}", now=now, repeat_hours=24.0):
            logger.warning("account %s: foreign-order alarm: %d open order(s) or position(s) the bot did not open (%s); not touched", account.id, len(findings), ", ".join(sorted({f.symbol for f in findings})))
            telemetry.log_decision(session, settings, account, stage=telemetry.STAGE_ENTRY, outcome=telemetry.OBSERVED, reason=foreign_orders.R_FOREIGN, is_paper=False, now=now,
                                   detail=dict(orders=sum(1 for f in findings if f.kind == "order"), positions=sum(1 for f in findings if f.kind == "position"), symbols=sorted({f.symbol for f in findings})[:6]))
            await _notify_safe(notifier, account, foreign_orders.message(findings))
    except Exception:  # noqa: BLE001
        logger.warning("account %s: foreign-order check failed this cycle", account.id)


async def _notify_safe(notifier: Notifier, account: TradingAccount, message: str) -> None:
    """Best-effort send for the new status notifications below - a failed
    delivery here must never crash or mislabel an otherwise-healthy cycle,
    mirroring the same pattern the Telegram webhook uses for its own replies."""
    try:
        await notifier.send(account.notification_target, message)
    except Exception:
        logger.exception("account %s: failed to send a status notification", account.id)


class _SafeNotifier:
    """Wraps a notifier so that a failed message is logged (through _notify_safe) and swallowed. The multi-trade cycle uses it so that one failed
    message cannot stop the other open trades from being managed (audit L8). It only ever sends to the account's own notification target."""

    def __init__(self, inner: Notifier, account: TradingAccount):
        self._inner, self._account = inner, account

    async def send(self, target, message) -> None:
        await _notify_safe(self._inner, self._account, message)


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


_LIMIT_CODES = {
    "structural": telemetry.R_PROTECTION_FAIL_LIMIT,
    "transient": telemetry.R_TRANSIENT_FAIL_LIMIT,
    "unclassified": telemetry.R_UNCLASSIFIED_FAIL_LIMIT,
}

# An observed PASS is logged at most this often per account and pair. Passes lead to an entry, so they are rare; this
# only stops a pass that keeps failing to open (for example a concurrent-trade refusal) from repeating every round.
_PASS_LOG_REPEAT_HOURS = 1.0


def _observe_line(account, settings: Settings, reason: str, now: datetime, message: str, *args) -> None:
    """One observe-mode journal line, repeated at most once per `protection_alert_repeat_hours` for the same account,
    pair and reason. The decision-log rows (when telemetry is on) are the complete, per-candle record."""
    if protection_guard.should_alert(
        account.id, account.pair, "observe:" + reason, now=now, repeat_hours=settings.protection_alert_repeat_hours
    ):
        logger.warning(message, *args)


def _limit_message(account: TradingAccount, hit) -> str:
    pair = account.pair
    if hit.kind == "transient":
        return (
            f"Binance could not be reached reliably enough to place a protective stop {hit.failures} times in the last "
            f"{hit.window_hours:g} hours, and each position was closed again straight away. New trades are paused for "
            "now and resume automatically when the connection to Binance has been stable. This is not caused by your "
            "settings."
        )
    if hit.kind == "unclassified":
        return (
            f"on {pair}, a protective stop could not be confirmed on Binance {hit.failures} times in the last "
            f"{hit.window_hours:g} hours, for a reason the bot could not identify, and each position was closed again "
            "straight away. New trades on this pair are paused for now."
        )
    return (
        f"on {pair}, Binance would not accept a protective stop {hit.failures} times in the last "
        f"{hit.window_hours:g} hours, and each position was closed again straight away. "
        "New trades on this pair are paused for now. Please check your Binance leverage and margin mode for "
        "this pair (a lower leverage usually fixes it)."
    )


async def _strategy_guards_block(session, account, settings: Settings, notifier: Notifier, signal, *, is_paper: bool, now: datetime, tlog, pair: str | None = None,
                                 strategy_id: str = strategy_guards.STRATEGY_ID, mode: str | None = None) -> bool:
    """The strategy guards (strategy_guards.py), reached only when STRATEGY_GUARDS_MODE is observe or enforce. True if a NEW entry is
    refused here (and the refusal has been recorded). The operator kill switch was checked before this and stays authoritative: this
    can only add a refusal. In observe mode nothing is blocked and nothing is written; in enforce mode the first guard that fires blocks
    the entry, a pause guard also writes its pause, and the owner is told once. An unreadable guard input refuses in enforce mode."""
    mode = mode or settings.strategy_guards_mode  # the new strategies pass NEW_STRATEGY_GUARDS_MODE; EMA keeps STRATEGY_GUARDS_MODE
    try:
        hits = strategy_guards.evaluate(session, account, settings, signal=signal, is_paper=is_paper, now=now, pair=pair, strategy_id=strategy_id)
    except Exception as exc:  # noqa: BLE001 - fail closed in enforce, silent in observe; never raise into the cycle
        logger.warning("account %s: strategy guards could not be evaluated (%s)", account.id, type(exc).__name__)
        if mode != "enforce":
            return False
        tlog(telemetry.REJECTED, strategy_guards.R_UNREADABLE, mode="enforce")
        await _block_entry(notifier, account, settings, strategy_guards.R_UNREADABLE, strategy_guards.MESSAGES[strategy_guards.R_UNREADABLE], now=now)
        return True
    if not hits:
        return False
    if mode == "observe":
        for hit in hits:
            tlog(telemetry.OBSERVED, hit.code, mode="observe", would_block=True, **hit.detail)
            _observe_line(account, settings, hit.code, now, "account %s: [observe] strategy guard %s would refuse a %s entry (%s); entry NOT blocked",
                          account.id, hit.code, account.pair, hit.detail)
        return False
    hit = hits[0]
    tlog(telemetry.REJECTED, hit.code, mode="enforce", **hit.detail)
    if hit.creates_pause:
        strategy_guards.create_pause(session, account.id, strategy_id, hit, is_paper=is_paper, now=now)
    if hit.owner_alert and (hit.creates_pause or protection_guard.should_alert(
            account.id, "-", hit.code + ":" + now.strftime("%Y-%m-%d"), now=now, repeat_hours=24.0)):
        await strategy_guards.alert_owner(settings, strategy_guards.owner_text(account.id, strategy_id, hit))
    message = hit.message
    if message is None:  # a signal filter: logged like the flat-market filter, no customer message
        logger.info("account %s: new %s entry skipped (%s)", account.id, account.pair, hit.code)
    else:
        await _block_entry(notifier, account, settings, hit.code, message, now=now)
    return True


async def _kill_switch_blocks(session, settings: Settings, notifier: Notifier, account: TradingAccount, tlog, *, now: datetime, stage: str) -> bool:
    """True if the operator kill switch stops a NEW live entry here (and the refusal has been recorded). `stage` says which
    of the checks fired. Reads are retried for a few seconds before the fail-closed rule applies (see kill_switch)."""
    decision = await kill_switch.acheck(session, settings)
    if not decision.engaged:
        if decision.attempts > 1:  # the answer was right, but the read needed retries: say so
            kill_switch.log_decision(decision, scope=account.id, now=now, repeat_hours=settings.protection_alert_repeat_hours)
        return False
    await _kill_switch_refuse(notifier, account, settings, decision, tlog, now=now, stage=stage)
    return True


async def _kill_switch_refuse(notifier: Notifier, account: TradingAccount, settings: Settings, decision, tlog, *, now: datetime, stage: str) -> None:
    """A refusal because of the kill switch is NOT a fault: it does not touch last_error. The customer is told at most once
    per `protection_alert_repeat_hours` for this account and pair, in fixed wording that never contains the operator's reason."""
    tlog(telemetry.REJECTED, telemetry.R_KILL_SWITCH, path=decision.path, check=stage, attempts=decision.attempts)
    logger.warning(
        "account %s: new %s entry refused (kill switch, path=%s, check=%s, attempts=%d)",
        account.id, account.pair, decision.path, stage, decision.attempts,
    )
    if protection_guard.should_alert(
        account.id, account.pair, "kill_switch", now=now, repeat_hours=settings.protection_alert_repeat_hours
    ):
        await _notify_safe(notifier, account, kill_switch.CUSTOMER_MESSAGE)


async def _block_entry(
    notifier: Notifier, account: TradingAccount, settings: Settings, reason: str, message: str, *, now: datetime
) -> None:
    """A safety refusal that can repeat every round while a signal persists. The dashboard message is always
    refreshed; the customer is alerted at most once per `protection_alert_repeat_hours` for the same
    account, pair and reason (plus once after a restart), and the refusal is always logged."""
    logger.warning("account %s: new %s entry refused (%s)", account.id, account.pair, reason)
    account.last_error = message
    account.last_error_at = now
    if protection_guard.should_alert(
        account.id, account.pair, reason, now=now, repeat_hours=settings.protection_alert_repeat_hours
    ):
        await _notify_safe(notifier, account, f"MBA Bot did not open a trade: {message}")


async def _precheck_entry(client, venue, settings: Settings, *, side, entry_price, stop_price, quantity, leverage, margin_mode, effective_capital):
    """Reads (read-only) what the pre-entry protection check needs and returns the verdict. Never raises: any
    missing or unreadable input becomes a refusal reason, so an unreadable exchange can never wave an entry through."""
    try:
        tiers = await exchange.fetch_maintenance_tiers(client, venue.symbol)
        other_positions = await exchange.fetch_open_position_count(client) if margin_mode == "cross" else 0
        return assess_stop_viability(
            side=side, entry_price=entry_price, stop_price=stop_price, quantity=quantity, leverage=leverage,
            effective_capital=effective_capital, margin_mode=margin_mode, tiers=tiers,
            other_open_positions=other_positions, min_maint_rate=settings.precheck_min_maint_rate,
            safety_fraction=settings.precheck_stop_safety_fraction,
            cross_collateral_factor=settings.precheck_cross_collateral_factor,
            max_leverage=settings.precheck_max_leverage,
        )
    except Exception:  # noqa: BLE001
        logger.exception("protection pre-check failed unexpectedly; refusing the entry")
        return StopViability(False, risk_module.INPUTS_INVALID)


def _precheck_message(account: TradingAccount, check: StopViability, leverage: float) -> str:
    pair = account.pair
    if check.reason == risk_module.STOP_UNVIABLE:
        return (
            f"on {pair} your Binance leverage ({leverage:g}x) puts liquidation too close for a protective stop to "
            f"work (the stop would sit about {check.stop_distance_frac * 100:.2f}% from entry, at most about "
            f"{check.max_allowed_stop_frac * 100:.2f}% is safe). Changing the leverage for this pair on Binance "
            "to a lower number allows trading it again."
        )
    if check.reason == risk_module.TIERS_UNAVAILABLE:
        return (
            f"Binance's margin tiers for {pair} could not be read or matched just now, so the bot could not confirm a "
            "protective stop is safe. This will retry automatically on the next check."
        )
    if check.reason == risk_module.MARGIN_MODE_UNKNOWN:
        return (
            f"Binance did not report whether {pair} uses isolated or cross margin, so the bot could not confirm a "
            "protective stop is safe. This will retry automatically on the next check."
        )
    if check.reason == risk_module.CROSS_UNVERIFIABLE:
        return (
            f"{pair} is set to cross margin and your Binance account holds other open positions (or they could not be "
            "read), so a safe stop distance cannot be confirmed. Close the other positions or switch this pair to "
            "isolated margin on Binance."
        )
    return "the stop-safety check could not be completed from the available data, so no trade was opened."


def _resolve_credentials(account: TradingAccount, settings: Settings, *, is_paper: bool, is_demo: bool = False) -> tuple[str | None, str | None]:
    if is_demo:
        return demo_module.credentials(account, settings)  # the customer's own demo keys, never the live credential
    if is_paper:
        # Spot Testnet and Futures Demo Trading are separate Binance systems
        # with separate key registrations - a key from one is not valid on
        # the other, so which pair to use depends on market_type, not
        # just "is this paper mode". (The legacy TESTNET_FUTURES_* are never used.)
        if account.market_type == "futures":
            return settings.demo_futures_api_key, settings.demo_futures_api_secret
        return settings.testnet_spot_api_key, settings.testnet_spot_api_secret
    credential = account.credential
    if credential is None:
        return None, None
    return (
        decrypt(credential.encrypted_api_key, key=settings.encryption_key),
        decrypt(credential.encrypted_api_secret, key=settings.encryption_key),
    )


def _note_small_capital(session, account, venue, settings: Settings, *, price, capital, reason: str, now, is_paper: bool = False) -> None:
    """SMALL_CAPITAL_PAIRS only: record why the account's own pair was skipped, with the capital its smallest order needs at the customer's leverage cap. Read-only against the exchange (the rules
    are already loaded), writes one counter row, never raises into the entry decision."""
    if not settings.small_capital_pairs or is_paper:
        return
    try:
        cap = small_capital.leverage_cap_for(account.market_type, leverage_module.account_cap(account.max_leverage, settings.futures_max_leverage))
        v = small_capital.assess(venue.rules, price, capital=capital, leverage_cap=cap)
        small_capital.record_skip(session, account, account.pair, reason, v, now=now)
    except Exception:  # noqa: BLE001
        logger.warning("account %s: could not record the pair skip", account.id)


async def _scan_auto_pair(
    client, account: TradingAccount, settings: Settings, *, capital: float | None = None, scan_out: dict | None = None, now_ms: int | None = None, viability_out: list | None = None
) -> tuple[str, list, StrategySignal] | None:
    """Auto Select's market scan. Reuses the SAME exchange client
    already built for this account this cycle - same environment, same
    credentials, same account - just queried against each pair in the
    fixed, approved universe (`config.auto_pairs` - always the original
    four; the extra Futures pairs are manual-only - the one list
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

    `scan_out`, when given, is filled with what the scan saw (pairs skipped
    for capital, candidates dropped by the flat-market filter, the ranking)
    for research telemetry only. It never influences the result.

    Returns None if nothing in the universe had an actionable signal
    this cycle - never a forced/fallback pick."""
    scanned: list[PairCandidate] = []
    raw: dict[str, list] = {}
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if capital is not None:
        try:
            await client.load_markets()  # cached on the client; the candle fetches below reuse it
        except Exception:
            capital = None  # cannot read the exchange's minimums: scan exactly as before
    for pair in auto_pairs(account.market_type):
        if capital is not None and viability_out is not None:
            # SMALL_CAPITAL_PAIRS: judge each pair by the exchange's own minimum order at the customer's leverage cap; a pair the account cannot afford is recorded and left out.
            try:
                rules = rules_from_market((client.markets or {}).get(exchange.exchange_symbol(pair, account.market_type)) or {})
            except Exception:  # noqa: BLE001
                rules = None
            cap = small_capital.leverage_cap_for(account.market_type, leverage_module.account_cap(account.max_leverage, settings.futures_max_leverage))
            v = small_capital.assess(rules, None, capital=capital, leverage_cap=cap)
            if v.viable is False:
                viability_out.append((pair, v))
                if scan_out is not None:
                    scan_out.setdefault("skipped_capital", []).append(pair)
                continue
        elif capital is not None:
            facts = pair_catalog.facts_from_market((client.markets or {}).get(exchange.exchange_symbol(pair, account.market_type)))
            if pair_catalog.capital_supports_market(capital, facts.min_notional, settings.max_notional_multiplier) is False:
                logger.info("account %s: auto-select skipping %s: %s", account.id, pair, pair_catalog.REASON_INSUFFICIENT_CAPITAL)
                if scan_out is not None:
                    scan_out.setdefault("skipped_capital", []).append(pair)
                continue
        try:
            ohlcv = await client.fetch_ohlcv(
                exchange.exchange_symbol(pair, account.market_type), timeframe=settings.timeframe, limit=settings.ohlcv_limit
            )
        except Exception:
            logger.warning("account %s: auto-select scan could not fetch %s this cycle, skipping it", account.id, pair)
            continue
        candles = candles_from_ohlcv(ohlcv)
        if candles:
            raw[pair] = candles  # the caller prices the entry from the raw last row
            scanned.append(PairCandidate(pair=pair, candles=_ema_signal_candles(candles, settings, now_ms)))  # ranking and signal use closed candles when the switch is on

    ranked = rank_candidates(
        scanned,
        market_type=account.market_type,
        ema_fast_period=settings.ema_fast_period,
        ema_slow_period=settings.ema_slow_period,
        atr_period=settings.atr_period,
        atr_flat_threshold=settings.atr_flat_threshold,
        atr_rolling_window=settings.atr_rolling_window,
    )
    if scan_out is not None:
        _note_scan(scan_out, scanned, ranked, settings)
    if not ranked:
        return None

    best_pair = ranked[0].pair
    signal = _ema_generate(next(c.candles for c in scanned if c.pair == best_pair), settings)
    return best_pair, raw[best_pair], signal


def _scan_info(scan_out: dict, result) -> telemetry.ScanInfo | None:
    """Telemetry only: summarise an Auto scan; never raises."""
    try:
        if result is None:
            return None
        picked = result[0]
        ranked = [(p, float(sc)) for p, sc in scan_out.get("ranked", [])]
        names = [p for p, _ in ranked]
        rank = names.index(picked) + 1 if picked in names else 1
        score = dict(ranked).get(picked)
        return telemetry.ScanInfo(
            picked=picked,
            rank=rank,
            score=score,
            ranked=ranked,
            skipped_capital=list(scan_out.get("skipped_capital", [])),
            flat_filtered=[p for p, _ in scan_out.get("flat_filtered", [])],
        )
    except Exception:  # noqa: BLE001
        return None


def _scan_events(scan_out: dict) -> list[dict]:
    try:
        events = [{"pair": p, "reason": telemetry.R_AUTO_SKIP_CAPITAL} for p in scan_out.get("skipped_capital", [])]
        events += [{"pair": p, "reason": telemetry.R_AUTO_FLAT, "candle_ts": ts} for p, ts in scan_out.get("flat_filtered", [])]
        return events
    except Exception:  # noqa: BLE001
        return []


def _note_scan(scan_out: dict, scanned: list, ranked: list, settings: Settings) -> None:
    """Telemetry only (see telemetry.py): never raises, never changes the scan."""
    try:
        scan_out["ranked"] = [(r.pair, r.score) for r in ranked]
        in_rank = {r.pair for r in ranked}
        flat = []
        for cand in scanned:
            if cand.pair in in_rank:
                continue
            sig = generate_signal(
                cand.candles,
                ema_fast_period=settings.ema_fast_period,
                ema_slow_period=settings.ema_slow_period,
                atr_period=settings.atr_period,
                atr_flat_threshold=settings.atr_flat_threshold,
                atr_rolling_window=settings.atr_rolling_window,
            )
            if sig.signal == SignalType.NONE and "flat" in (sig.reason or ""):
                flat.append((cand.pair, int(cand.candles[-1].timestamp)))
        scan_out["flat_filtered"] = flat
    except Exception:  # noqa: BLE001
        pass


async def _maybe_enter(session, account, client, signal, current_price, settings, notifier, *, is_paper, now, tctx=None, pair=None, multi=None, is_demo=False):
    pair = pair or account.pair  # a multi-trade paper account enters on the pair it scanned; every other caller passes none
    is_rsi = isinstance(signal, rsi_mr.RsiSignal)
    is_don = isinstance(signal, donchian.DonchianSignal)
    strategy_id = rsi_mr.STRATEGY_ID if is_rsi else (donchian.STRATEGY_ID if is_don else multi_trade.EMA_STRATEGY_ID)
    if is_don:
        # donchian_breakout_v1 is PAPER ONLY IN CODE: a live account never runs it, whatever DONCHIAN_PAPER_ONLY or any other setting says.
        if (not is_paper or not settings.donchian_enabled or donchian.STRATEGY_ID not in multi_trade.account_strategies(account)
                or signal.side not in ("long", "short") or signal.stop is None):
            return
        if signal.side == "short" and account.market_type != "futures":
            return
        side = signal.side
    elif is_rsi:
        # rsi_mr_v1 is PAPER ONLY IN CODE: a live account never runs it, whatever RSI_MR_PAPER_ONLY or any other setting says.
        if (not is_paper or not settings.rsi_mr_enabled or rsi_mr.STRATEGY_ID not in multi_trade.account_strategies(account)
                or signal.side not in ("long", "short")):
            return
        if signal.side == "short" and account.market_type != "futures":
            return
        side = signal.side
    elif signal.signal == SignalType.BULLISH_CROSSOVER:
        side = "long"
    elif signal.signal == SignalType.BEARISH_CROSSOVER and account.market_type == "futures":
        side = "short"
    else:
        return

    def _tlog(outcome: str, reason: str, **detail) -> None:
        # Research telemetry only; no-op unless TRADE_TELEMETRY is on. Never raises.
        telemetry.log_decision(
            session, settings, account, stage=telemetry.STAGE_ENTRY, outcome=outcome, reason=reason,
            is_paper=is_paper, ctx=tctx, now=now, detail=detail or None, pair=pair, strategy_id=strategy_id,
        )

    if not is_paper and await _kill_switch_blocks(session, settings, notifier, account, _tlog, now=now, stage="early"):
        return
    if not is_paper and not is_demo and await _account_loss_blocks(session, account, client, settings, notifier, _tlog, now=now):
        return  # account-wide daily loss pause: no NEW live entry today; open trades keep their stops and exits

    if pair not in manual_pairs(account.market_type, futures_expansion=settings.futures_pair_expansion):
        # Fail closed: never open a NEW position on a pair that is not currently selectable for
        # this market (e.g. an extra Futures pair after the expansion was switched off). Managing
        # an already-open trade never goes through here - it always uses the trade's own pair.
        _tlog(telemetry.REJECTED, telemetry.R_PAIR_UNAVAILABLE)
        await _record_error(
            notifier, account, "This pair is not currently available for trading, so no new trade was opened.", now=now
        )
        return

    new_strategy = is_rsi or is_don
    guards_mode = settings.new_strategy_guards_mode if new_strategy else settings.strategy_guards_mode
    if new_strategy and (guards_mode != "enforce" or settings.fee_guard_margin <= 0):
        # Fail closed (audit M1): these strategies trade only when THEIR guard mode is enforce (observe blocks nothing, so it would let an unguarded
        # strategy trade) and the fee guard is on. Logged once per day, not every cycle.
        prefix = "rsi" if is_rsi else "donchian"
        code = (f"{prefix}_guards_off" if guards_mode == "off" else f"{prefix}_guards_not_enforced" if guards_mode == "observe" else f"{prefix}_fee_guard_off")
        _tlog(telemetry.REJECTED, code)
        if protection_guard.should_alert(account.id, "-", f"{code}:{now.strftime('%Y-%m-%d')}", now=now, repeat_hours=24.0):
            logger.warning("account %s: %s skipped (%s): set NEW_STRATEGY_GUARDS_MODE=enforce and FEE_GUARD_MARGIN above 0", account.id, strategy_id, code)
        return

    if guards_mode != "off" and await _strategy_guards_block(
        session, account, settings, notifier, signal, is_paper=is_paper, now=now, tlog=_tlog, pair=pair, strategy_id=strategy_id, mode=guards_mode
    ):
        return

    if is_don:
        # The channel midline or 1.5 ATR, whichever is closer to entry (donchian.initial_stop). Refused if it is not on the losing side of the entry.
        stop_price = signal.stop
        if not (stop_price == stop_price and stop_price > 0 and ((side == "long" and stop_price < current_price) or (side == "short" and stop_price > current_price))):
            _tlog(telemetry.REJECTED, "donchian_stop_invalid")
            return
    else:
        stop_price = compute_stop_loss(
            entry_price=current_price, atr=signal.atr, side=side,
            atr_multiplier=settings.rsi_mr_stop_atr if is_rsi else settings.atr_stop_multiplier,
        )
    risk_pct_used = (min(account.risk_pct, settings.rsi_mr_risk_pct) if is_rsi else
                     min(account.risk_pct, settings.donchian_risk_pct) if is_don else account.risk_pct)

    if is_paper:
        # Paper mode keeps its own pre-existing sizing base unchanged -
        # the fixed, synthetic paper_balance IS the sandbox capital, and
        # was never the subject of the "entire exchange balance" finding
        # this allocation model fixes (that only ever applied to the
        # live branch's fetch_available_quote_balance call below).
        effective_capital = account.paper_balance
    else:
        # ENTRY-CAPABLE credential validation, fresh on every entry and
        # independent of anything checked earlier in the cycle: a key that can
        # withdraw never reaches an order, and a durable block (set the first
        # time one was seen) holds until the credential is replaced.
        if account.withdrawal_blocked_at is not None:
            _tlog(telemetry.REJECTED, telemetry.R_WITHDRAWAL_BLOCKED)
            return
        hits = (
            []
            if settings.protection_fail_limit_mode == "off"
            else protection_guard.limit_hits(
                session, account.id, account.pair, now=now, settings=settings, codes=_LIMIT_CODES
            )
        )
        for hit in hits:
            # A protective stop could not be established `hit.failures` times inside the window (see
            # protection_guard for the categories and scopes). Stop re-entering until the oldest leaves the window.
            limit_detail = dict(
                kind=hit.kind, failures=hit.failures, limit=hit.limit, window_hours=hit.window_hours, scope=hit.scope
            )
            if settings.protection_fail_limit_mode == "observe":
                _tlog(telemetry.OBSERVED, hit.reason_code, mode="observe", would_block=True, **limit_detail)
                _observe_line(
                    account, settings, hit.reason_code, now,
                    "account %s: [observe] %s protection-failure limit reached on %s (%d in %gh, limit %d); entry NOT blocked",
                    account.id, hit.kind, account.pair, hit.failures, hit.window_hours, hit.limit,
                )
            else:
                _tlog(telemetry.REJECTED, hit.reason_code, mode="enforce", **limit_detail)
                await _block_entry(notifier, account, settings, hit.reason_code, _limit_message(account, hit), now=now)
                return
        try:
            if not is_demo:  # demo keys are not real keys: the withdrawal-permission read (a production-only endpoint) is skipped for them, never for live
                await exchange.assert_trade_only(client)
        except exchange.WithdrawalPermissionDetected:
            _tlog(telemetry.REJECTED, telemetry.R_WITHDRAWAL_DETECTED)
            await _flag_withdrawal_block(session, account, notifier, now=now)
            return

        if account.allocated_capital is None:
            # Fail closed: Trading Capital must be an explicit customer
            # choice before any live entry, never a silent "use whatever
            # Binance shows" fallback (see models.py's allocated_capital
            # docstring). resolve_trading_mode's Start Bot gate already
            # keeps an account with no allocation out of LIVE entirely,
            # so this should be unreachable in practice - this check is
            # deliberate defense in depth, not redundancy to be trimmed.
            logger.warning("account %s: live account has no allocated_capital configured, refusing to enter", account.id)
            _tlog(telemetry.REJECTED, telemetry.R_NO_CAPITAL_SET)
            return
        live_balance = await exchange.fetch_available_quote_balance(client, account.pair)
        # The customer's configured ceiling, never more than what
        # Binance actually shows as free right now - see models.py's
        # allocated_capital docstring and the capital-allocation spec:
        # effective_capital = min(configured_trading_capital, currently_available_balance).
        effective_capital = min(account.allocated_capital, live_balance)
        if effective_capital <= 0:
            _tlog(telemetry.REJECTED, telemetry.R_NO_FREE_BALANCE, allocated=account.allocated_capital, free=live_balance)
            return

    size = compute_position_size(
        account_balance=effective_capital,
        risk_pct=risk_pct_used,
        entry_price=current_price,
        stop_price=stop_price,
        max_risk_pct=settings.risk_pct_max,
    )
    if size.quantity <= 0:
        _tlog(telemetry.REJECTED, telemetry.R_SIZE_ZERO)
        return

    if multi is not None and is_paper:
        size, refusal = _multi_paper_limits(session, account, settings, size, current_price, multi)
        if refusal is not None:
            code, detail = refusal
            _tlog(telemetry.REJECTED, code, **detail)
            logger.info("account %s: multi-trade entry on %s skipped (%s) %s", account.id, pair, code, detail)
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
            client,
            market_type=account.market_type,
            pair=account.pair,
            spot_stop_limit_buffer_pct=settings.spot_stop_limit_buffer_pct,
            entries_allowed=True,  # the ONLY place: assert_trade_only just passed above
        )
        ctx = lifecycle.LiveContext(
            session=session, account=account, venue=venue, settings=settings, notifier=notifier, now=now, is_demo=is_demo
        )
        try:
            await venue.prepare()
            await lifecycle.preflight_entry(ctx)
        except lifecycle.EntryRefused as exc:
            _tlog(telemetry.REJECTED, telemetry.R_PREFLIGHT_REFUSED)
            await _record_error(notifier, account, exc.message, now=now)
            return
        except (exchange.UnsupportedAccountMode, exchange.UnsupportedSymbol) as exc:
            _tlog(telemetry.REJECTED, telemetry.R_UNSUPPORTED)
            await _record_error(notifier, account, exchange.safe_error_message(exc), now=now)
            return
        except ProtectionUnavailable:
            _tlog(telemetry.REJECTED, telemetry.R_NO_PROTECTION)
            await _record_error(
                notifier,
                account,
                "An exchange-side protective stop cannot currently be placed for this pair on Binance, so no new "
                "live trade was opened. This will retry automatically on the next check.",
                now=now,
            )
            return

        leverage = None  # recorded by telemetry only; Spot has none
        margin_mode = None
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
            leverage, margin_mode = await exchange.fetch_symbol_leverage_and_margin_mode(client, venue.symbol, side=side)
            if leverage is None:
                _tlog(telemetry.REJECTED, telemetry.R_LEVERAGE_UNREADABLE)
                await _record_error(
                    notifier,
                    account,
                    "Could not verify this account's Binance Futures leverage/margin just now, so no new live "
                    "trade was opened. This will retry automatically on the next check.",
                    now=now,
                )
                return
            sizing_leverage = leverage  # the multiple the position value is capped at; below, the pre-entry check still judges the REAL Binance leverage
            if _leverage_mode(settings, now) == leverage_module.MODE_SIZE_CAP:
                # size_cap (docs/safety/customer_leverage.md): never refuse for a high Binance number; size to min(Binance, the customer's number). Isolated margin is still
                # required, and the liquidation check below still uses the real Binance leverage. The bot only READS leverage and margin mode.
                if margin_mode != "isolated":
                    _tlog(telemetry.REJECTED, telemetry.R_FUTURES_NOT_ISOLATED, margin_mode=margin_mode, mode="size_cap")
                    await _block_entry(notifier, account, settings, telemetry.R_FUTURES_NOT_ISOLATED, leverage_module.MSG_NOT_ISOLATED.format(pair=account.pair), now=now)
                    return
                cap = leverage_module.account_cap(account.max_leverage, settings.futures_max_leverage)
                sizing_leverage = leverage_module.effective(leverage, cap)
                if leverage > cap:
                    logger.info("account %s: size_cap: position value capped at %gx capital (customer setting %dx, Binance %gx)", account.id, sizing_leverage, cap, leverage)
            elif settings.futures_max_leverage > 0:
                # Global ceiling (docs/safety/account_daily_loss.md): the bot only READS leverage and margin mode and refuses; it never sets either on Binance.
                # Checked before the pre-entry protection check below, which then judges the stop against the liquidation price.
                if leverage > settings.futures_max_leverage + 1e-9:
                    _tlog(telemetry.REJECTED, telemetry.R_FUTURES_LEVERAGE_CEILING, leverage=leverage, ceiling=settings.futures_max_leverage)
                    await _block_entry(notifier, account, settings, telemetry.R_FUTURES_LEVERAGE_CEILING,
                                       f"your Binance Futures leverage on {account.pair} ({leverage:g}x) is above the limit of {settings.futures_max_leverage:g}x set for this service. "
                                       "Lower it for this pair on Binance. MBA Bot never changes it for you.", now=now)
                    return
                if margin_mode != "isolated":
                    _tlog(telemetry.REJECTED, telemetry.R_FUTURES_NOT_ISOLATED, margin_mode=margin_mode)
                    await _block_entry(notifier, account, settings, telemetry.R_FUTURES_NOT_ISOLATED,
                                       f"your Binance Futures margin mode on {account.pair} is not isolated (or could not be read), and this service trades isolated margin only. "
                                       "Switch the pair to isolated margin on Binance. MBA Bot never changes it for you.", now=now)
                    return
            size = apply_margin_ceiling(
                size, entry_price=current_price, effective_capital=effective_capital, leverage=sizing_leverage
            )
        if size.quantity <= 0:
            _tlog(telemetry.REJECTED, telemetry.R_SIZE_ZERO_CEILING)
            _note_small_capital(session, account, venue, settings, price=current_price, capital=effective_capital, reason=small_capital.R_BELOW_MIN_CAPITAL, now=now, is_paper=is_paper)
            return
        quantity = venue.plan_entry_quantity(size.quantity, entry_price=current_price, stop_price=stop_price, side=side)
        if quantity <= 0:
            logger.info("account %s: sized quantity cannot satisfy %s exchange filters/protection, no entry", account.id, venue.symbol)
            _tlog(
                telemetry.REJECTED, telemetry.R_EXCHANGE_FILTERS,
                wanted_qty=size.quantity, wanted_notional=size.quantity * current_price, capital=effective_capital,
            )
            _note_small_capital(session, account, venue, settings, price=current_price, capital=effective_capital, reason=small_capital.R_BELOW_FILTERS, now=now, is_paper=is_paper)
            return
        precheck_detail = None
        if account.market_type == "futures" and settings.protection_precheck_mode != "off":
            check = await _precheck_entry(
                client, venue, settings, side=side, entry_price=current_price, stop_price=stop_price, quantity=quantity,
                leverage=leverage, margin_mode=margin_mode, effective_capital=effective_capital,
            )
            precheck_detail = check.detail()
            if not check.viable:
                detail = dict(
                    leverage=leverage, side=side, mode=settings.protection_precheck_mode,
                    min_maint_rate=settings.precheck_min_maint_rate, safety_fraction=settings.precheck_stop_safety_fraction,
                    cross_collateral_factor=settings.precheck_cross_collateral_factor, **precheck_detail,
                )
                if settings.protection_precheck_mode == "observe":
                    _tlog(telemetry.OBSERVED, check.reason, would_block=True, **detail)
                    _observe_line(
                        account, settings, check.reason, now,
                        "account %s: [observe] protection pre-check would refuse %s %s (%s) %s; entry NOT blocked",
                        account.id, side, account.pair, check.reason, precheck_detail,
                    )
                else:
                    _tlog(telemetry.REJECTED, check.reason, **detail)
                    logger.warning("account %s: protection pre-check refused (%s) %s", account.id, check.reason, precheck_detail)
                    await _block_entry(notifier, account, settings, check.reason, _precheck_message(account, check, leverage), now=now)
                    return
            elif settings.protection_precheck_mode == "observe" and protection_guard.should_alert(
                account.id, account.pair, "observe:pass", now=now, repeat_hours=_PASS_LOG_REPEAT_HOURS
            ):
                # Journal record of a PASS (the decision log carries it on the accepted row when telemetry is on), so
                # observe mode is measurable without telemetry. One line per entry, not per round.
                logger.info(
                    "account %s: [observe] protection pre-check passes %s %s at %gx: stop %.3f%% of entry, allowed "
                    "%.3f%%, bracket %s (effective maintenance rate %.3f%%), %s margin",
                    account.id, side, account.pair, leverage, check.stop_distance_frac * 100,
                    check.max_allowed_stop_frac * 100, check.tier, check.effective_maint_rate * 100, check.margin_basis,
                )
        # The steps above made several exchange calls: read the kill switch again right before the order.
        if await _kill_switch_blocks(session, settings, notifier, account, _tlog, now=now, stage="before_order"):
            return
        risk_amount = quantity * size.stop_distance
        try:
            opened = await lifecycle.open_live_trade(
                ctx,
                side=side,
                quantity=quantity,
                signal_price=current_price,
                stop_price=stop_price,
                risk_text=f"Risk: {risk_amount:.2f} ({account.risk_pct:.2f}% of balance)",
            )
        except kill_switch.EntryPaused as paused:
            await _kill_switch_refuse(notifier, account, settings, paused.decision, _tlog, now=now, stage="in_lifecycle")
            return
        if opened is None:
            _tlog(telemetry.REJECTED, telemetry.R_CONCURRENT_TRADE)
        elif telemetry.enabled(settings):
            def _record_live() -> None:
                _tlog(telemetry.ACCEPTED, telemetry.R_ENTRY_SUBMITTED, trade_status=opened.status, precheck=precheck_detail)
                telemetry.record_entry(
                    session, settings, account, opened, ctx=tctx, is_paper=False, effective_capital=effective_capital,
                    risk_amount=risk_amount, quantity=quantity, stop_price=stop_price, leverage=leverage,
                )

            telemetry.write_and_commit(session, _record_live)
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
        pair=pair,
        strategy_id=strategy_id,
    )
    session.add(trade)
    if tctx is not None:
        session.flush()  # assigns trade.id so the telemetry row can reference it; same transaction, committed just below

        def _record_paper() -> None:
            _tlog(telemetry.ACCEPTED, telemetry.R_ENTRY_PAPER, trade_id=trade.id)
            telemetry.record_entry(
                session, settings, account, trade, ctx=tctx, is_paper=True, effective_capital=effective_capital,
                risk_amount=size.risk_amount, quantity=size.quantity, stop_price=stop_price,
            )

        telemetry.write_with_trade(session, _record_paper)
    session.commit()  # durable before the (network) notification, and never held across it

    mode_label = "PAPER" if is_paper else "LIVE"
    await notifier.send(
        account.notification_target,
        f"[{mode_label}] Entered {side.upper()} {pair}\n"
        f"Entry: {current_price:.4f}  Qty: {size.quantity:.6f}  Stop: {stop_price:.4f}\n"
        f"Risk: {size.risk_amount:.2f} ({risk_pct_used:.2f}% of balance)"
        + (f"\nStrategy: {strategy_id}  Open trades: {multi.n_open + 1} of {multi.cap}" if multi is not None else ""),
    )


# ====================================================================== multi-trade (PAPER accounts; docs/safety/multi_trade.md)


@dataclass(frozen=True)
class MultiInfo:
    """What the multi-trade cycle tells _maybe_enter: how many slots, how many already used, and why (the grade)."""
    cap: int
    n_open: int
    grade: multi_trade.Grade


def _use_multi(account: TradingAccount, settings: Settings, is_paper: bool, open_trades: list[Trade]) -> bool:
    """True when this cycle must handle more than one trade. With the defaults (MAX_OPEN_POSITIONS 1, no pair_list) and any live
    account this is False unless the database already holds more than one non-terminal trade for the account (which is then managed,
    never ignored)."""
    if len(open_trades) > 1:
        return True
    if open_trades and all(t.is_paper for t in open_trades) and any(multi_trade.strategy_of(t) in multi_trade.NEW_STRATEGY_IDS for t in open_trades):
        # A paper trade that rsi_mr_v1 or donchian_breakout_v1 opened is managed by ITS OWN rules, even if its switch was turned off since and even if the account
        # now resolves as LIVE (audit L1; refusing would leave the trade with no exit). It runs on the paper environment, places no order and enters nothing new
        # (a live-mode account never enters from this path).
        return True
    if not is_paper or any(not t.is_paper for t in open_trades):
        return False
    opted = multi_trade.account_strategies(account)
    if (settings.donchian_enabled and donchian.STRATEGY_ID in opted) or (settings.rsi_mr_enabled and rsi_mr.STRATEGY_ID in opted):
        return True  # the new strategies run only on this path, only for PAPER accounts the owner opted in (audit H2); others keep the EMA path and Auto Select
    return multi_trade.configured_cap(settings.max_open_positions) > 1 and len(multi_trade.trading_pairs(account)) > 1


def _market_grade(session, account: TradingAccount, settings: Settings, candles, *, now: datetime) -> multi_trade.Grade:
    """Gather the grade's inputs for a PAPER account and grade the market. Any failure to read an input is grade 1."""
    try:
        highs, lows, closes = [c.high for c in candles], [c.low for c in candles], [c.close for c in candles]
        values = atr_series(highs, lows, closes, settings.atr_period)
        cur = values[-1]
        pcts = [a / c for a, c in zip(values[-settings.atr_rolling_window:], closes[-settings.atr_rolling_window:]) if a is not None and c]
        atr_pct = (cur / closes[-1]) if cur is not None and closes[-1] else None
        avg = (sum(pcts) / len(pcts)) if pcts else None
        fee_bps = settings.fee_guard_fee_bps
        costs = strategy_guards.costs_for(settings, account.market_type)
        need = max(settings.grade2_min_trades, settings.grade3_min_trades)
        recent = strategy_guards.closed_results(session, account.id, is_paper=True, fee_bps=fee_bps, limit=need, costs=costs)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = strategy_guards.closed_results(session, account.id, is_paper=True, fee_bps=fee_bps, since=day_start - timedelta(microseconds=1), costs=costs)
        paused = any(strategy_guards.is_active(p, now) for p in strategy_guards.pauses(session, account.id, strategy_guards.STRATEGY_ID, True))
        inputs = multi_trade.GradeInputs(
            atr_pct=atr_pct, atr_avg_pct=avg, flat_threshold=settings.atr_flat_threshold,
            nets_newest_first=tuple(r.net for r in recent), pause_active=paused, daily_pnl=sum(r.net for r in today),
            capital=account.paper_balance, daily_loss_limit_pct=settings.strategy_daily_loss_pct,
        )
        return multi_trade.grade_from(inputs, multi_trade.GradeThresholds(
            grade2_min_trades=settings.grade2_min_trades, grade3_min_trades=settings.grade3_min_trades,
            extreme_atr_multiple=settings.grade_extreme_atr, daily_loss_fraction=settings.grade_daily_loss_fraction,
        ))
    except Exception as exc:  # noqa: BLE001 - an unreadable input is grade 1, never an assumption that the market is fine
        logger.warning("account %s: market grade could not be computed (%s); grade 1", account.id, type(exc).__name__)
        return multi_trade.Grade(1, ("input_error",))


def _multi_paper_limits(session, account: TradingAccount, settings: Settings, size, current_price: float, multi: MultiInfo):
    """Checks for a 2nd or 3rd PAPER entry: slot still free, free capital, minimum order size, total open risk. Returns
    (size, None) to go ahead, possibly with the quantity trimmed to the free capital, or (size, (reason_code, detail)) to skip.
    The first trade of an account is never touched by this."""
    balance = account.paper_balance
    slots = multi_trade.configured_cap(settings.max_open_positions)
    if slots > 1:
        # Owner decision (audit L9): with more than one slot configured a single trade may use at most 1/slots of the balance as notional (a third for
        # three slots), the first trade included. With one slot nothing changes.
        size = apply_exposure_ceiling(size, entry_price=current_price, effective_capital=balance, max_notional_multiplier=1.0 / slots)
        if not size.quantity > 0:
            return size, (multi_trade.R_CAPITAL_SHORT, {"balance": round(balance, 2), "slots": slots})
    open_ts = list_open_trades(session, account.id)
    if not open_ts:
        return size, None
    if len(open_ts) >= multi.cap:
        return size, (multi_trade.R_SLOT_FULL, {"open": len(open_ts), "cap": multi.cap, "grade": multi.grade.level})
    open_pct = multi_trade.open_risk_pct([abs(t.entry_price - t.stop_loss_price) * t.quantity for t in open_ts], balance)
    if open_pct is None:
        return size, (multi_trade.R_TOTAL_RISK_CAP, {"reason": "open risk unreadable"})  # fail closed
    free = multi_trade.free_capital(balance, [t.entry_price * t.quantity for t in open_ts])
    if not free > 0:
        return size, (multi_trade.R_CAPITAL_SHORT, {"free": round(free, 2)})
    size = apply_exposure_ceiling(size, entry_price=current_price, effective_capital=free, max_notional_multiplier=1.0)
    notional = size.quantity * current_price
    if size.quantity <= 0 or notional < settings.paper_min_notional:
        return size, (multi_trade.R_CAPITAL_SHORT, {"free": round(free, 2), "notional": round(notional, 2), "minimum": settings.paper_min_notional})
    hit = strategy_guard_rules.portfolio_hit(
        open_positions=len(open_ts), open_risk_pct=open_pct, new_risk_pct=size.risk_amount / balance * 100.0,
        max_positions=0, max_total_risk_pct=multi_trade.open_risk_cap(settings.max_total_risk_pct, account.risk_pct, slots),  # max(MAX_TOTAL_RISK_PCT, risk x slots), cap 100
    )
    if hit is not None:
        return size, (multi_trade.R_TOTAL_RISK_CAP, dict(hit.detail))
    return size, None


async def _process_multi(session, account, client, open_trades: list[Trade], settings, notifier, *, is_paper: bool, now) -> None:
    notifier = _SafeNotifier(notifier, account)  # one failed message must not stop the other trades being managed (audit L8)
    live = [t for t in open_trades if not t.is_paper]
    if live:
        await _process_multi_live(session, account, client, live, settings, notifier, now=now)
        return

    pairs = multi_trade.trading_pairs(account)
    cache: dict[str, list] = {}

    async def candles_for(pair: str):
        if pair not in cache:
            ohlcv = await client.fetch_ohlcv(
                exchange.exchange_symbol(pair, account.market_type), timeframe=settings.timeframe, limit=settings.ohlcv_limit
            )
            cache[pair] = candles_from_ohlcv(ohlcv)
        return cache[pair]

    now_ms = int(now.timestamp() * 1000)

    def signal_for(candles):
        """The EMA signal for a pair's raw candles; with EMA_CLOSED_CANDLE_ONLY on, from the closed ones only. Prices stay from the raw list."""
        return _ema_generate(_ema_signal_candles(candles, settings, now_ms), settings)

    opted = multi_trade.account_strategies(account)
    rsi_on = settings.rsi_mr_enabled and rsi_mr.STRATEGY_ID in opted
    rsi_p = _rsi_params(settings)
    rsi_tf, _ = rsi_mr.normalize_timeframe(settings.rsi_mr_timeframe)  # the same value is asked of the exchange and used for the closed-candle test
    rsi_tf_ms = rsi_mr.timeframe_ms(rsi_tf)
    rsi_cache: dict[str, list] = {}

    async def rsi_closed_for(pair: str):
        if pair not in rsi_cache:
            ohlcv = await client.fetch_ohlcv(
                exchange.exchange_symbol(pair, account.market_type), timeframe=rsi_tf, limit=settings.ohlcv_limit
            )
            rsi_cache[pair] = rsi_mr.closed_only(candles_from_ohlcv(ohlcv), now_ms, rsi_tf_ms)
        return rsi_cache[pair]

    don_on = settings.donchian_enabled and donchian.STRATEGY_ID in opted
    don_p = _donchian_params(settings)
    don_tf, _ = rsi_mr.normalize_timeframe(settings.donchian_timeframe)
    don_tf_ms = rsi_mr.timeframe_ms(don_tf)
    don_cache: dict[str, list] = {}

    async def don_closed_for(pair: str):
        if pair not in don_cache:
            ohlcv = await client.fetch_ohlcv(
                exchange.exchange_symbol(pair, account.market_type), timeframe=don_tf, limit=settings.ohlcv_limit
            )
            don_cache[pair] = rsi_mr.closed_only(candles_from_ohlcv(ohlcv), now_ms, don_tf_ms)
        return don_cache[pair]

    # 1. Manage every open trade on its own pair, whatever the grade is, with the rules of the strategy that opened it.
    for t in open_trades:
        pair = t.pair or account.pair
        if multi_trade.strategy_of(t) == donchian.STRATEGY_ID:
            await _manage_donchian_trade(session, account, t, await don_closed_for(pair), settings, notifier, p=don_p, tf_ms=don_tf_ms, now=now)
            continue
        if multi_trade.strategy_of(t) == rsi_mr.STRATEGY_ID:
            await _manage_rsi_trade(session, account, t, await rsi_closed_for(pair), settings, notifier, p=rsi_p, tf_ms=rsi_tf_ms, now=now)
            continue
        candles = await candles_for(pair)
        if not candles:
            logger.warning("account %s: no OHLCV data returned for %s", account.id, pair)
            await _record_error(notifier, account, "No market data was returned for this account's pair just now.", now=now)
            continue
        await _maybe_exit(session, account, client, t, signal_for(candles), candles[-1].close, notifier, is_paper=True, now=now,
                          settings=settings, multi_label=True)

    # 2. At most ONE new entry per cycle, only if the grade leaves a slot.
    open_now = list_open_trades(session, account.id)
    taken = {t.pair or account.pair for t in open_now}
    free_pairs = [p for p in pairs if p not in taken]
    primary = await candles_for(pairs[0])
    if primary:
        sig0 = signal_for(primary)
        account.last_signal = sig0.signal.value
        account.last_signal_at = now
        account.last_error = None
        account.last_error_at = None
    if free_pairs and primary and is_paper:
        grade = _market_grade(session, account, settings, primary, now=now)
        cap = multi_trade.slot_cap(is_paper=is_paper, configured=settings.max_open_positions, grade=grade.level)
        if len(open_now) >= cap:
            if protection_guard.should_alert(account.id, "-", f"multi:slots_full:{len(open_now)}:{cap}", now=now, repeat_hours=6.0):
                logger.info("account %s: %d of %d trade slots used, %s; no new entry", account.id, len(open_now), cap, grade.text())
        else:
            # Strategy priority when two signal at once (docs/safety/donchian_breakout_v1.md): Donchian, then EMA, then RSI. The first
            # strategy that has a signal AND is not refused (kill switch, guards, caps) takes the slot; a strategy that is refused
            # falls through to the next one. One new entry per cycle in total.
            order = ([donchian.STRATEGY_ID] if don_on else []) + [multi_trade.EMA_STRATEGY_ID] + ([rsi_mr.STRATEGY_ID] if rsi_on else [])
            allow_short = account.market_type == "futures"

            window_ms = int(settings.new_strategy_entry_window_minutes * 60_000)

            def fresh(strategy: str, pair: str, closed: list, tf_ms: int) -> bool:
                """Audit M5: take the entry only if the signal candle is the latest closed candle and closed within the window; otherwise skip and log."""
                age_ms = now_ms - (closed[-1].timestamp + tf_ms)
                if 0 <= age_ms <= window_ms:
                    return True
                if protection_guard.should_alert(account.id, pair, f"stale:{strategy}:{closed[-1].timestamp}", now=now, repeat_hours=24.0):
                    logger.info("account %s: %s signal on %s skipped (signal_stale): the signal candle closed %.0f minutes ago, the window is %g",
                                account.id, strategy, pair, age_ms / 60_000, settings.new_strategy_entry_window_minutes)
                    telemetry.log_decision(session, settings, account, stage=telemetry.STAGE_ENTRY, outcome=telemetry.SKIPPED, reason="signal_stale", is_paper=True,
                                           now=now, pair=pair, strategy_id=strategy, detail={"age_minutes": round(age_ms / 60_000, 1), "window_minutes": settings.new_strategy_entry_window_minutes})
                return False

            async def signal_of(strategy: str, pair: str):
                """(signal object, price, candles) for one strategy on one pair, or None if it has no actionable signal."""
                if strategy == donchian.STRATEGY_ID:
                    closed = await don_closed_for(pair)
                    sig = donchian.signal_from_candles(closed, don_p, allow_short=allow_short)
                    return (sig, closed[-1].close, closed) if (sig.side and closed and fresh(strategy, pair, closed, don_tf_ms)) else None
                if strategy == rsi_mr.STRATEGY_ID:
                    closed = await rsi_closed_for(pair)
                    sig = rsi_mr.generate_signal(closed, rsi_p, allow_short=allow_short)
                    return (sig, closed[-1].close, closed) if (sig.side and closed and fresh(strategy, pair, closed, rsi_tf_ms)) else None
                candles = await candles_for(pair)
                if not candles:
                    return None
                sig = signal_for(candles)
                if sig.signal == SignalType.NONE:
                    return None
                ema_closed = _ema_signal_candles(candles, settings, now_ms)  # the list the signal was made from (the raw one with the switch off)
                if _ema_entry_stale(session, settings, account, sig, ema_closed, pair, now=now, now_ms=now_ms, is_paper=True):
                    return None
                return sig, candles[-1].close, ema_closed

            for position, strategy in enumerate(order):
                for pair in free_pairs:
                    found = await signal_of(strategy, pair)
                    if found is None:
                        continue
                    signal, price, candles = found
                    tctx = None
                    if strategy == donchian.STRATEGY_ID:
                        telemetry.log_decision(session, settings, account, stage=telemetry.STAGE_SIGNAL, outcome=telemetry.ACCEPTED,
                                               reason=telemetry.R_SIGNAL_ACTIONABLE, is_paper=True, now=now, pair=pair, strategy_id=strategy,
                                               detail={"upper": signal.upper, "lower": signal.lower, "volume_ratio": (signal.volume / signal.avg_volume) if signal.avg_volume else None,
                                                       "atr_pct": signal.atr_pct})
                    elif strategy == rsi_mr.STRATEGY_ID:
                        telemetry.log_decision(session, settings, account, stage=telemetry.STAGE_SIGNAL, outcome=telemetry.ACCEPTED,
                                               reason=telemetry.R_SIGNAL_ACTIONABLE, is_paper=True, now=now, pair=pair, strategy_id=strategy,
                                               detail={"rsi": signal.rsi, "adx": signal.adx, "atr_pct": signal.atr_pct})
                    elif telemetry.enabled(settings):
                        tctx = telemetry.build_entry_context(candles, signal, settings, now=now, scan=None)
                        telemetry.signal_stage(session, settings, account, tctx, signal, is_paper=True, now=now, pair=pair)
                    await _maybe_enter(session, account, client, signal, price, settings, notifier, is_paper=True, now=now,
                                       tctx=tctx, pair=pair, multi=MultiInfo(cap=cap, n_open=len(open_now), grade=grade))
                    if len(list_open_trades(session, account.id)) > len(open_now):
                        for later in order[position + 1:]:  # a lower-priority strategy that also had a signal on this pair loses the slot
                            if await signal_of(later, pair) is not None:
                                logger.info("account %s: strategy_slot_lost_to: %s signal on %s not taken, %s took the slot", account.id, later, pair, strategy)
                                telemetry.log_decision(session, settings, account, stage=telemetry.STAGE_ENTRY, outcome=telemetry.SKIPPED,
                                                       reason="strategy_slot_lost_to", is_paper=True, now=now, pair=pair, strategy_id=later,
                                                       detail={"winner": strategy})
                        break
                else:
                    continue  # no entry by this strategy: the next one gets its turn
                break  # one new entry per cycle
    session.commit()
    await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)


def _donchian_params(settings: Settings) -> donchian.DonchianParams:
    return donchian.DonchianParams(entry_period=settings.donchian_entry_period, exit_period=settings.donchian_exit_period, volume_period=settings.donchian_entry_period,
                                   volume_mult=settings.donchian_volume_mult, atr_extreme_mult=settings.donchian_atr_extreme_mult,
                                   stop_atr=settings.donchian_stop_atr, trail_atr=settings.donchian_trail_atr)


async def _manage_donchian_trade(session, account, trade: Trade, closed: list, settings: Settings, notifier, *, p: donchian.DonchianParams, tf_ms: int, now) -> None:
    """Exit a PAPER donchian_breakout_v1 trade by its own rules (stop, trailing stop, close beyond the opposite channel) from the closed
    candles that ended after it was opened. The result depends only on the candles, not on when this cycle ran."""
    if not trade.is_paper:
        return  # paper only: a live trade is never managed here
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    ex = donchian.exit_check(side=trade.side, entry=trade.entry_price, stop=trade.stop_loss_price, opened_ms=int(opened.timestamp() * 1000),
                             closed=closed, tf_ms=tf_ms, p=p)
    if ex is None:
        return
    pnl = (ex.price - trade.entry_price) * trade.quantity if trade.side == "long" else (trade.entry_price - ex.price) * trade.quantity
    trade.exit_price = ex.price
    trade.pnl = pnl
    trade.status = "closed"
    trade.closed_at = now
    trade.close_reason = ex.reason
    account.paper_balance += pnl
    session.commit()  # durable before the (network) notification
    pair = trade.pair or account.pair
    await notifier.send(
        account.notification_target,
        f"[PAPER] Exited {trade.side.upper()} {pair} ({ex.reason})\n"
        + pnl_labels.exit_line(trade.entry_price, ex.price, pnl, on=bool(settings.pnl_labels), paper=True, quote=pnl_labels.quote_of(pair))
        + f"\nStrategy: {donchian.STRATEGY_ID}",
    )


def _rsi_params(settings: Settings) -> rsi_mr.RsiParams:
    return rsi_mr.RsiParams(adx_max=settings.rsi_mr_adx_max, stop_atr=settings.rsi_mr_stop_atr, target_atr=settings.rsi_mr_target_atr,
                            time_stop_candles=settings.rsi_mr_time_stop_candles)


async def _manage_rsi_trade(session, account, trade: Trade, closed: list, settings: Settings, notifier, *, p: rsi_mr.RsiParams, tf_ms: int, now) -> None:
    """Exit a PAPER rsi_mr_v1 trade by its own rules (stop, profit target, RSI back to 50, time stop) from the closed candles that ended
    after it was opened. The result depends only on the candles, not on when this cycle ran."""
    if not trade.is_paper:
        return  # paper only: a live trade is never managed here
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    ex = rsi_mr.exit_check(side=trade.side, entry=trade.entry_price, stop=trade.stop_loss_price, opened_ms=int(opened.timestamp() * 1000),
                           closed=closed, tf_ms=tf_ms, p=p)
    if ex is None:
        return
    pnl = (ex.price - trade.entry_price) * trade.quantity if trade.side == "long" else (trade.entry_price - ex.price) * trade.quantity
    trade.exit_price = ex.price
    trade.pnl = pnl
    trade.status = "closed"
    trade.closed_at = now
    trade.close_reason = ex.reason
    account.paper_balance += pnl
    session.commit()  # durable before the (network) notification
    pair = trade.pair or account.pair
    await notifier.send(
        account.notification_target,
        f"[PAPER] Exited {trade.side.upper()} {pair} ({ex.reason})\n"
        + pnl_labels.exit_line(trade.entry_price, ex.price, pnl, on=bool(settings.pnl_labels), paper=True, quote=pnl_labels.quote_of(pair))
        + f"\nStrategy: {rsi_mr.STRATEGY_ID}",
    )


async def _process_multi_live(session, account, client, live_trades: list[Trade], settings, notifier, *, now) -> None:
    """More than one non-terminal LIVE trade exists. Code never opens that (a live account holds one), so this is a recovery path:
    reconcile and monitor EACH live trade on its own pair, open nothing new. A paper trade on the same account is left for later."""
    logger.warning("account %s: %d non-terminal live trades on one account; managing each, no new entries", account.id, len(live_trades))
    for t in live_trades:
        t, ctx = await _manage_live_trade(session, account, client, t, settings, notifier, now=now)
        if t.status != TRADE_OPEN or t.protection_status != PROTECTION_PROTECTED:
            continue  # closed this cycle, or unhealthy and worked by the reconciler: no strategy decision on unverified protection
        pair = t.pair or account.pair
        ohlcv = await client.fetch_ohlcv(exchange.exchange_symbol(pair, account.market_type), timeframe=settings.timeframe, limit=settings.ohlcv_limit)
        candles = candles_from_ohlcv(ohlcv)
        if not candles:
            logger.warning("account %s: no OHLCV data returned for %s", account.id, pair)
            continue
        signal = _ema_generate(_ema_signal_candles(candles, settings, int(now.timestamp() * 1000)), settings)
        opposite = (t.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (t.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER)
        await lifecycle.monitor_trade(ctx, t, crossover_exit=opposite, current_price=candles[-1].close)
    account.last_error = None
    account.last_error_at = None
    await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)


async def _maybe_exit(session, account, client, open_trade: Trade, signal, current_price, notifier, *, is_paper, now, settings=None, multi_label=False):
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
    session.commit()  # durable before the (network) notification; also makes it visible to the daily summary

    mode_label = "PAPER"
    await notifier.send(
        account.notification_target,
        f"[{mode_label}] Exited {open_trade.side.upper()} {open_trade.pair or account.pair} ({open_trade.close_reason})\n"
        + pnl_labels.exit_line(
            open_trade.entry_price, exit_price, pnl, on=bool(settings and settings.pnl_labels), paper=True,
            quote=pnl_labels.quote_of(open_trade.pair or account.pair),
        )
        + (f"\nStrategy: {multi_trade.strategy_of(open_trade)}" if multi_label else ""),
    )


async def _flag_withdrawal_block(session, account, notifier, *, now) -> None:
    """Record, durably and once, that the stored key can withdraw, and alert
    the customer once. Committed BEFORE the alert so a crash or restart never
    repeats it (at-most-once; the dashboard shows the block regardless)."""
    if account.withdrawal_blocked_at is not None:
        return
    account.withdrawal_blocked_at = now
    session.commit()
    logger.warning("account %s: stored key has withdrawal permission; new entries blocked", account.id)
    if account.notification_target is not None:
        try:
            await notifier.send(account.notification_target, exchange.WITHDRAWAL_BLOCK_MESSAGE)
        except Exception:
            logger.exception("account %s: failed to send the withdrawal-permission alert", account.id)


async def _manage_live_trade(session, account, client, trade: Trade, settings, notifier, *, now, is_demo: bool = False):
    """Verify the exchange can be managed safely, then reconcile `trade`
    against Binance. Returns (trade, LiveContext)."""
    venue = build_venue(
        client,
        market_type=account.market_type,
        pair=trade.pair or account.pair,
        spot_stop_limit_buffer_pct=settings.spot_stop_limit_buffer_pct,
    )
    ctx = lifecycle.LiveContext(session=session, account=account, venue=venue, settings=settings, notifier=notifier, now=now, is_demo=is_demo or bool(trade.is_demo))
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
    pair: str | None = None,
) -> None:
    """Reconcile this account's live trade (if any) against Binance and do
    nothing else - no scan, no entry. Used at startup and by the fast
    recovery loop (scheduler.py); the main loop does the same inside
    process_account. With `current_price` (the fast stop-limit watch) the
    protection-health check also runs: if the price has gone through a
    resting stop-limit's executable limit, the lifecycle cancels it,
    confirms, and market-closes the remainder."""
    live_trades = [t for t in list_open_trades(session, account.id) if not t.is_paper]
    if current_price is not None and pair is not None:
        live_trades = [t for t in live_trades if (t.pair or account.pair) == pair]  # the fast watch's price belongs to one pair
    if not live_trades:
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
        # Risk-reducing management of an existing position: allowed even with a
        # withdrawal-capable key (it is flagged and the customer alerted once),
        # because this path's venue cannot place an entry.
        if await exchange.has_withdrawal_permission(client):
            await _flag_withdrawal_block(session, account, notifier, now=now)
        # Normally one trade. A live account is capped at one in code, but if more than one non-terminal live trade exists
        # (a manual database change, a lowered cap) every one of them is reconciled: none is skipped, none is orphaned.
        for trade in live_trades:
            trade, ctx = await _manage_live_trade(session, account, client, trade, settings, notifier, now=now)
            if current_price is not None:
                await lifecycle.monitor_trade(ctx, trade, crossover_exit=False, current_price=current_price)
    finally:
        await client.close()


async def _maybe_send_daily_summary(session, account, notifier, *, now, settings=None) -> None:
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
        pnl_labels.daily_summary(
            today, len(closed_today), total_pnl, quote, on=bool(settings and settings.pnl_labels),
            paper_count=sum(1 for t in closed_today if t.is_paper),
        ),
    )
