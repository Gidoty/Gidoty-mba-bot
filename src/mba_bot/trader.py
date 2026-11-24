"""One evaluation cycle for one customer account.

`process_account` is the unit of work the scheduler fans out over every
active account each poll interval. It is written to fail closed and
isolated: any exception here is caught by the caller (scheduler.py) so one
customer's bad data or exchange error never stops another customer's loop.
"""
from __future__ import annotations

import dataclasses

import logging
import sys
from dataclasses import dataclass, replace as dc_replace
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, object_session

from . import ema_exits, ema_reversal, fee_tier, reentry, profit_live, profit_protect, opportunity, account_loss, daily_limit as daily_limit_module, demo as demo_module, donchian_trail, live_multi, reporting, small_capital, donchian, exchange, foreign_orders, kill_switch, leverage as leverage_module, risk_limits as risk_limits_module, lifecycle, multi_trade, pair_catalog, rsi_mr, pnl_labels, protection_guard, strategy_guard_rules, strategy_guards, telemetry
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


def _reentry_signal(session, account, settings: Settings, signal, sig_candles: list, pair: str, *, now_ms: int, is_paper: bool):
    """Part J (docs/safety/reentry.md), single-slot accounts, off by default. When the EMA strategy shows NO crossover, decide whether ONE re-entry should be tried: the same pair and side as the account's last closed
    EMA trade, if that trade left through a protective exit and the trend has resumed (reentry.decide). Returns (a crossover-shaped StrategySignal for the ordinary `_maybe_enter`, the previous trade) or None. It never
    enters anything itself: the caller sends the signal through the same guards, risk, stop, protection check and fee guard as any entry. A flat market, a stale candle (older than EMA_ENTRY_WINDOW_MINUTES) and a trade
    of any other strategy never qualify. Never raises."""
    try:
        rules = reentry.rules_from_settings(settings)
        if not rules.enabled or signal.signal != SignalType.NONE or "flat" in (signal.reason or "") or not sig_candles:
            return None
        tf, _ = rsi_mr.normalize_timeframe(settings.timeframe)
        tf_ms = rsi_mr.timeframe_ms(tf)
        closed = rsi_mr.closed_only(sig_candles, now_ms, tf_ms)
        if not closed:
            return None
        age_ms = now_ms - (closed[-1].timestamp + tf_ms)
        if not 0 <= age_ms <= int(settings.ema_entry_window_minutes * 60_000):
            return None  # the same freshness rule as a crossover entry
        prev = session.query(Trade).filter(Trade.account_id == account.id, Trade.status == "closed", Trade.is_paper == is_paper, Trade.pair == pair, Trade.closed_at.is_not(None)).order_by(Trade.closed_at.desc()).first()
        if prev is None or multi_trade.strategy_of(prev) != multi_trade.EMA_STRATEGY_ID or (prev.side == "short" and account.market_type != "futures"):
            return None
        closed_at = prev.closed_at if prev.closed_at.tzinfo else prev.closed_at.replace(tzinfo=timezone.utc)
        since = sum(1 for c in closed if c.timestamp >= int(closed_at.timestamp() * 1000))  # closed candles that STARTED after the exit
        links, cur = 0, prev
        while cur is not None and cur.reentry_of and links < 10:
            links += 1
            cur = session.get(Trade, cur.reentry_of)
        ok, _why = reentry.decide(rules=rules, side=prev.side, prev_reason=prev.close_reason, prev_exit_price=prev.exit_price, candles_since_exit=since, reentries_so_far=links,
                                  fast=signal.fast_ema, slow=signal.slow_ema, last_close=closed[-1].close, atr=signal.atr)
        if not ok:
            return None
        kind = SignalType.BULLISH_CROSSOVER if prev.side == "long" else SignalType.BEARISH_CROSSOVER
        return dataclasses.replace(signal, signal=kind, reason=f"re-entry after {prev.close_reason}"), prev
    except Exception:  # noqa: BLE001 - a re-entry decision must never stop the cycle
        logger.exception("account %s: the re-entry check failed; no re-entry is tried", getattr(account, "id", "?"))
        return None


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
    opportunity.record_not_taken(session, settings, account, strategy_id=multi_trade.EMA_STRATEGY_ID, pair=pair, candle_ts=last_ts, reason=opportunity.R_STALE, is_paper=is_paper, now=now,
                                 detail={"age_minutes": round(age_ms / 60_000, 1), "window_minutes": settings.ema_entry_window_minutes})  # observability only
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
            await _process_multi(session, account, client, open_trades, settings, notifier, is_paper=is_paper, now=now, is_demo=is_demo)
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
            rsi_trade = multi_trade.strategy_of(open_trade) == rsi_mr.STRATEGY_ID
            don_trade = multi_trade.strategy_of(open_trade) == donchian.STRATEGY_ID
            opposite_cross = not rsi_trade and not don_trade and (
                (open_trade.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (
                    open_trade.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER)
            )  # an RSI trade is exited by its own rules below, never by an EMA crossover
            _track_peak(session, open_trade, candles, candles[-1].close, now_ms=now_ms)  # the best price since entry (observation only)
            await _giveback_alert(session, account, open_trade, candles[-1].close, settings, now=now)  # one owner alert, alert only (off by default)
            await lifecycle.monitor_trade(ctx, open_trade, crossover_exit=opposite_cross, current_price=candles[-1].close)
            if not rsi_trade and not don_trade and open_trade.status == TRADE_OPEN:
                await _live_ema_manage(ctx, open_trade, settings, now_ms=now_ms, candles=candles, current_price=candles[-1].close)  # take profit, trailing stop, break-even (all off by default)
            if rsi_trade and open_trade.status == TRADE_OPEN:
                await _live_rsi_exit(ctx, open_trade, client, settings, now_ms=now_ms, current_price=candles[-1].close)
            if don_trade and open_trade.status == TRADE_OPEN:
                await _live_donchian_manage(ctx, open_trade, client, settings, now_ms=now_ms, current_price=candles[-1].close)
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
        auto_new: list[str] = []
        auto_cache = None
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
            ledger_rows: list | None = [] if opportunity.enabled(settings) else None
            auto_new = _auto_new_strategies(settings, account, is_paper)  # [] for every account that has not opted in: then nothing below differs from before
            auto_cache: dict | None = {} if auto_new else None
            auto_result = await _scan_auto_pair(client, account, settings, capital=auto_capital, scan_out=scan_out, now_ms=now_ms, viability_out=viability_out, ledger_rows=ledger_rows, candle_out=auto_cache)
            if ledger_rows is not None:
                _ledger_auto_scan(session, settings, account, ledger_rows, auto_result, is_paper=is_paper, now=now, now_ms=now_ms)  # observability only
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
                evaluated: set[str] = set()
                if auto_new:
                    # No EMA signal: the strategies this account opted in to are still evaluated on every candidate pair of this round (Donchian first, then RSI; EMA has nothing to enter).
                    for sid in auto_new:
                        if list_open_trades(session, account.id):
                            break  # the one live slot is taken
                        if await _auto_new_strategy_pass(session, account, client, settings, notifier, strategy=sid, cache=auto_cache or {}, now=now, now_ms=now_ms, is_demo=is_demo):
                            evaluated.add(sid)
                if ledger_rows is not None:
                    _ledger_not_evaluated(session, settings, account, is_paper=is_paper, now=now, now_ms=now_ms, evaluated=evaluated)  # a strategy that could not be evaluated is still said so (observability only)
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
            if open_trade is None and sig_candles:
                opportunity.record_verdict(session, settings, account, strategy_id=multi_trade.EMA_STRATEGY_ID, pair=account.pair, candle_ts=sig_candles[-1].timestamp, sig=signal,
                                           price=sig_candles[-1].close, is_paper=is_paper, now=now, tf=settings.timeframe)  # observability only

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
            don_took_it = False
            if not is_paper and settings.live_donchian:
                if donchian.STRATEGY_ID in auto_new:
                    await _auto_new_strategy_pass(session, account, client, settings, notifier, strategy=donchian.STRATEGY_ID, cache=auto_cache or {}, now=now, now_ms=now_ms, is_demo=is_demo)  # Auto Select: every candidate pair
                else:
                    await _live_donchian_entry(session, account, client, settings, notifier, now=now, now_ms=now_ms, is_demo=is_demo)  # highest priority: Donchian, then EMA, then RSI
                don_took_it = bool(list_open_trades(session, account.id))
            if don_took_it:
                pass  # the one live slot is taken; EMA and RSI wait for the next cycle
            elif _ema_entry_stale(session, settings, account, signal, sig_candles, scan_pair, now=now, now_ms=now_ms, is_paper=is_paper):
                pass  # a closed-candle crossover that is too old to enter at a fair price; logged once per candle
            else:
                entry_signal, prev_trade = signal, None
                again = _reentry_signal(session, account, settings, signal, sig_candles, scan_pair, now_ms=now_ms, is_paper=is_paper)  # off by default; an ordinary entry below, same guards
                if again is not None:
                    entry_signal, prev_trade = again
                known_ids = {t.id for t in list_open_trades(session, account.id)}
                await _maybe_enter(
                    session, account, client, entry_signal, current_price, settings, notifier, is_paper=is_paper, now=now, tctx=tctx, is_demo=is_demo
                )
                if prev_trade is not None:
                    for t in list_open_trades(session, account.id):
                        if t.id not in known_ids:
                            t.reentry_of = prev_trade.id  # the link to the trade this one followed (a record only)
                            session.commit()
            if not is_paper and settings.live_rsi_mr:
                if rsi_mr.STRATEGY_ID in auto_new:
                    if not list_open_trades(session, account.id):
                        await _auto_new_strategy_pass(session, account, client, settings, notifier, strategy=rsi_mr.STRATEGY_ID, cache=auto_cache or {}, now=now, now_ms=now_ms, is_demo=is_demo)  # Auto Select: every candidate pair
                else:
                    await _live_rsi_entry(session, account, client, settings, notifier, now=now, now_ms=now_ms, is_demo=is_demo)  # lowest priority: only if EMA took no entry
        else:
            # Only a PAPER trade reaches here: live trades returned above.
            _track_peak(session, open_trade, candles, current_price, now_ms=now_ms)  # the best price since entry (observation only)
            _paper_staged_ratchet(open_trade, rsi_mr.closed_only(candles, now_ms, rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(settings.timeframe)[0])), current_price, settings, tf_ms=rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(settings.timeframe)[0]), atr_period=settings.atr_period, now_ms=now_ms, account=account)
            _tfms = rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(settings.timeframe)[0])
            if await _paper_time_stop(session, account, open_trade, rsi_mr.closed_only(candles, now_ms, _tfms), current_price, settings, notifier, tf_ms=_tfms, atr_period=settings.atr_period, now_ms=now_ms, limit=settings.ema_max_hold_candles, now=now):
                await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)
                return
            _rev = _reversal_reason(open_trade, rsi_mr.closed_only(candles, now_ms, _tfms), settings, now_ms, current_price)
            if _rev is not None:
                await _paper_close_at_price(session, account, open_trade, current_price, _rev, "EMA reversal", settings, notifier, now=now)
                await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)
                return
            await _maybe_exit(
                session, account, client, open_trade, signal, current_price, notifier, is_paper=effective_is_paper, now=now,
                settings=settings, take_profit=_paper_ema_rules(open_trade, candles, settings, now_ms),
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
    account, pair and reason (kept in the database, so also across a restart or a second process), and the refusal is always logged."""
    logger.warning("account %s: new %s entry refused (%s)", account.id, account.pair, reason)
    account.last_error = message
    account.last_error_at = now
    # The last alert time is kept in the database, so a restart or a second scheduler process cannot send the same alert again inside the window (protection_guard.claim_alert).
    if protection_guard.claim_alert(
        object_session(account), account.id, account.pair, reason, now=now, repeat_hours=settings.protection_alert_repeat_hours
    ):
        await _notify_safe(notifier, account, f"MBA Bot did not open a trade: {message}")


async def _precheck_entry(client, venue, settings: Settings, *, side, entry_price, stop_price, quantity, leverage, margin_mode, effective_capital, cache_key=None):
    """Reads (read-only) what the pre-entry protection check needs and returns the verdict. Never raises: any
    missing or unreadable input becomes a refusal reason, so an unreadable exchange can never wave an entry through.
    The bracket read retries once and may use the last known good table of this account and symbol (`cache_key`) for at most
    PRECHECK_TIERS_CACHE_MAX_AGE_SECONDS, only when the fresh read failed (exchange.read_maintenance_tiers)."""
    try:
        read = await exchange.read_maintenance_tiers(client, venue.symbol, cache_key=cache_key, cache_max_age=settings.precheck_tiers_cache_max_age_seconds)
        other_positions = await exchange.fetch_open_position_count(client) if margin_mode == "cross" else 0
        check = assess_stop_viability(
            side=side, entry_price=entry_price, stop_price=stop_price, quantity=quantity, leverage=leverage,
            effective_capital=effective_capital, margin_mode=margin_mode, tiers=read.tiers,
            other_open_positions=other_positions, min_maint_rate=settings.precheck_min_maint_rate,
            safety_fraction=settings.precheck_stop_safety_fraction,
            cross_collateral_factor=settings.precheck_cross_collateral_factor,
            max_leverage=settings.precheck_max_leverage,
        )
        note = check.note
        if check.reason == risk_module.TIERS_READ_FAILED:
            note = f"{read.cause}: {read.detail}"[:300]  # the real cause of the failed read (already cleaned of keys and long tokens)
        elif check.reason in (risk_module.TIERS_INVALID, risk_module.TIERS_NO_MATCH):
            exchange.warn_tiers(venue.symbol, check.reason, note or "")
        return dc_replace(check, note=note, tiers_source=(read.source if read.tiers is not None else None), tiers_age_seconds=read.age_seconds)
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
    if check.reason == risk_module.TIERS_READ_FAILED:
        return (
            f"the bot could not get the position-size safety table Binance publishes for {pair} just now (a connection or Binance problem, not your settings), "
            "so it could not confirm a protective stop is safe and did not open a trade. It checks again on the next cycle while the signal lasts."
        )
    if check.reason == risk_module.TIERS_INVALID:
        return (
            f"the position-size safety table Binance sent for {pair} was incomplete or did not add up, so the bot could not confirm a protective stop is safe "
            "and did not open a trade. This is not caused by your settings. It checks again on the next cycle while the signal lasts."
        )
    if check.reason == risk_module.TIERS_NO_MATCH:
        size = f" ({check.notional:,.2f} USDT)" if check.notional else ""
        return (
            f"the position-size safety table Binance published for {pair} does not cover the size of this trade{size}, so the bot could not confirm a protective stop "
            "is safe and did not open a trade. It checks again on the next cycle while the signal lasts."
        )
    if check.reason == risk_module.TIERS_UNAVAILABLE:  # legacy code, no longer produced; kept for old rows
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
    client, account: TradingAccount, settings: Settings, *, capital: float | None = None, scan_out: dict | None = None, now_ms: int | None = None, viability_out: list | None = None,
    ledger_rows: list | None = None, candle_out: dict | None = None,
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
                if ledger_rows is not None:
                    ledger_rows.append(("skip", pair))  # observability only
                continue
        elif capital is not None:
            facts = pair_catalog.facts_from_market((client.markets or {}).get(exchange.exchange_symbol(pair, account.market_type)))
            if pair_catalog.capital_supports_market(capital, facts.min_notional, settings.max_notional_multiplier) is False:
                logger.info("account %s: auto-select skipping %s: %s", account.id, pair, pair_catalog.REASON_INSUFFICIENT_CAPITAL)
                if scan_out is not None:
                    scan_out.setdefault("skipped_capital", []).append(pair)
                if ledger_rows is not None:
                    ledger_rows.append(("skip", pair))  # observability only
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
            if candle_out is not None:
                candle_out[pair] = candles  # the candles of this round, in scan order, for the Donchian and RSI checks (no second fetch); None unless an opted-in account needs them
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
    if ledger_rows is not None:
        try:  # observability only: what the EMA rule said about every scanned pair, and which of them had an actionable signal
            for cand in scanned:
                if cand.candles:
                    ledger_rows.append(("eval", cand.pair, _ema_generate(cand.candles, settings), cand.candles[-1].timestamp, cand.candles[-1].close))
            ledger_rows.append(("ranked", [r.pair for r in ranked]))
        except Exception:  # noqa: BLE001
            pass
    if not ranked:
        return None

    best_pair = ranked[0].pair
    signal = _ema_generate(next(c.candles for c in scanned if c.pair == best_pair), settings)
    return best_pair, raw[best_pair], signal


def _auto_new_strategies(settings: Settings, account, is_paper: bool) -> list[str]:
    """The newer strategies an Auto Select LIVE account is opted in to AND the operator has switched on (both the strategy and its live switch), in the code's priority order: Donchian, then RSI (EMA sits
    between them and has its own scan). Empty for a paper account, a manual account and any account that has not opted in: for those nothing in the Auto Select flow differs from before."""
    if is_paper or account.pair_selection_mode != "auto":
        return []
    opted = multi_trade.account_strategies(account)
    out = []
    if settings.live_donchian and settings.donchian_enabled and donchian.STRATEGY_ID in opted:
        out.append(donchian.STRATEGY_ID)
    if settings.live_rsi_mr and settings.rsi_mr_enabled and rsi_mr.STRATEGY_ID in opted:
        out.append(rsi_mr.STRATEGY_ID)
    return out


async def _auto_new_strategy_pass(session, account, client, settings: Settings, notifier, *, strategy: str, cache: dict, now, now_ms: int, is_demo: bool) -> bool:
    """One opted-in strategy on an Auto Select account, evaluated on EVERY candidate pair of this round (the pairs the scan read, in the scan's order; candles come from the scan's own reads when the
    strategy's timeframe is the scan's, so no extra exchange read). Every evaluation is recorded in the opportunity ledger by `_live_strategy_signal`. If one or more pairs have a fresh signal, the FIRST pair in
    scan order is entered through `_maybe_enter` exactly as an EMA or single-pair entry is (same guards, risk, stop); the others are recorded as `not_best_ranked`. The account's pair is set to the entered pair
    for the entry and put back if no trade opened (an Auto Select pair only ever changes together with an entry). Returns True once the strategy was evaluated."""
    hits: list[tuple[str, object, float]] = []
    candle_of: dict[str, int] = {}  # observability only: the signal candle of each hit
    evaluated = False
    for pair in list(cache):
        meta: dict = {}
        try:
            got = await _live_strategy_signal(client, account, pair, strategy, settings, now=now, now_ms=now_ms, session=session, candles=cache.get(pair), meta=meta)
        except exchange.WithdrawalPermissionDetected:
            raise
        except Exception:  # noqa: BLE001 - a failed read on one pair must not stop the other pairs or the cycle
            logger.warning("account %s: %s check on %s failed this round, skipping that pair", account.id, strategy, pair)
            continue
        evaluated = True
        if got is not None:
            hits.append((pair, got[0], got[1]))
            if "candle_ts" in meta:
                candle_of[pair] = meta["candle_ts"]
    if not hits:
        return evaluated
    pair, sig, price = hits[0]  # the pick is the first pair in scan order; nothing below changes it
    picked_strength = opportunity.strength(strategy, sig)
    for other, other_sig, _p in hits[1:]:
        opportunity.record_not_taken(session, settings, account, strategy_id=strategy, pair=other, candle_ts=candle_of.get(other), reason=opportunity.R_NOT_BEST, is_paper=False, now=now,
                                     detail={"picked": pair, "picked_strength": picked_strength, "strength": opportunity.strength(strategy, other_sig)})  # observability only
    previous = account.pair
    account.pair = pair
    await _maybe_enter(session, account, client, sig, price, settings, notifier, is_paper=False, now=now, is_demo=is_demo)
    if not list_open_trades(session, account.id):
        account.pair = previous
    return True


def _ledger_auto_scan(session, settings: Settings, account, rows: list, result, *, is_paper: bool, now: datetime, now_ms: int) -> None:
    """Opportunity ledger rows for one Auto Select scan (observability only, never raises): the EMA verdict for every scanned pair, the pairs skipped for capital, and, when a pair was picked, the pairs that
    also had a signal but ranked lower (not_best_ranked)."""
    try:
        picked = result[0] if result is not None else None
        ts_by_pair: dict[str, int] = {}
        for row in rows:
            if row[0] == "skip":
                tf, _ = rsi_mr.normalize_timeframe(settings.timeframe)
                tf_ms = rsi_mr.timeframe_ms(tf)
                opportunity.record_not_taken(session, settings, account, strategy_id=multi_trade.EMA_STRATEGY_ID, pair=row[1], candle_ts=((now_ms // tf_ms) - 1) * tf_ms,
                                             reason=opportunity.R_CAPITAL_SKIP, is_paper=is_paper, now=now)
            elif row[0] == "eval":
                _, pair, sig, candle_ts, close = row
                ts_by_pair[pair] = candle_ts
                opportunity.record_verdict(session, settings, account, strategy_id=multi_trade.EMA_STRATEGY_ID, pair=pair, candle_ts=candle_ts, sig=sig, price=close, is_paper=is_paper, now=now, tf=settings.timeframe)
        for row in rows:
            if row[0] == "ranked" and picked is not None:
                for other in row[1]:
                    if other != picked:
                        opportunity.record_not_taken(session, settings, account, strategy_id=multi_trade.EMA_STRATEGY_ID, pair=other, candle_ts=ts_by_pair.get(other), reason=opportunity.R_NOT_BEST,
                                                     is_paper=is_paper, now=now, detail={"picked": picked})
    except Exception:  # noqa: BLE001
        pass


def _ledger_not_evaluated(session, settings: Settings, account, *, is_paper: bool, now: datetime, now_ms: int, evaluated: set | None = None) -> None:
    """For an opted-in Auto Select account with Donchian or RSI live, say in the ledger that a strategy was NOT evaluated this round, once per candle (observability only, never raises). Since Auto Select
    evaluates every opted-in strategy even when EMA found nothing (`_auto_new_strategy_pass`), this now only fires for a strategy that could not be evaluated (the one live slot was already taken, or the
    check failed); `evaluated` is the set of strategies that did run, which are never recorded here."""
    try:
        if is_paper:
            return
        opted = multi_trade.account_strategies(account)
        for sid, on, tfs in ((donchian.STRATEGY_ID, settings.live_donchian and settings.donchian_enabled, settings.donchian_timeframe), (rsi_mr.STRATEGY_ID, settings.live_rsi_mr and settings.rsi_mr_enabled, settings.rsi_mr_timeframe)):
            if on and sid in opted and sid not in (evaluated or ()):
                tf, _ = rsi_mr.normalize_timeframe(tfs)
                tf_ms = rsi_mr.timeframe_ms(tf)
                opportunity.record_not_taken(session, settings, account, strategy_id=sid, pair=account.pair, candle_ts=((now_ms // tf_ms) - 1) * tf_ms, reason=opportunity.R_NOT_EVALUATED, is_paper=False, now=now,
                                             detail={"why": "slot_taken_or_check_failed"})
    except Exception:  # noqa: BLE001
        pass


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
        # donchian_breakout_v1 is paper only unless LIVE_DONCHIAN is on (default off): then an opted-in LIVE or DEMO account may enter, with every guard an EMA entry has (docs/safety/live_donchian.md).
        if ((not is_paper and not settings.live_donchian) or not settings.donchian_enabled or donchian.STRATEGY_ID not in multi_trade.account_strategies(account)
                or signal.side not in ("long", "short") or signal.stop is None):
            return
        if signal.side == "short" and account.market_type != "futures":
            return
        side = signal.side
    elif is_rsi:
        # rsi_mr_v1 is paper only unless LIVE_RSI_MR is on (default off): then an opted-in LIVE or DEMO account may enter, with every guard an EMA entry has (docs/safety/live_rsi.md).
        if ((not is_paper and not settings.live_rsi_mr) or not settings.rsi_mr_enabled or rsi_mr.STRATEGY_ID not in multi_trade.account_strategies(account)
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
                session, account.id, pair, now=now, settings=settings, codes=_LIMIT_CODES
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
                    account.id, hit.kind, pair, hit.failures, hit.window_hours, hit.limit,
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
        live_balance = await exchange.fetch_available_quote_balance(client, pair)
        # The customer's configured ceiling, never more than what
        # Binance actually shows as free right now - see models.py's
        # allocated_capital docstring and the capital-allocation spec:
        # effective_capital = min(configured_trading_capital, currently_available_balance).
        effective_capital = min(account.allocated_capital, live_balance)
        open_live = [t for t in list_open_trades(session, account.id) if not t.is_paper] if settings.live_multi_position else []
        if open_live:
            # A second or third live trade (LIVE_MULTI_POSITION): the trading capital is shared, so this trade is sized from what the open trades have not already used.
            cap_for_share = small_capital.leverage_cap_for(account.market_type, leverage_module.account_cap(account.max_leverage, settings.futures_max_leverage))
            effective_capital = min(effective_capital, live_multi.free_capital(account.allocated_capital, open_live, cap_for_share))
            if effective_capital <= 0:
                _tlog(telemetry.REJECTED, live_multi.R_CAPITAL_SHORT, allocated=account.allocated_capital, open_trades=len(open_live))
                return
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
            pair=pair,
            spot_stop_limit_buffer_pct=settings.spot_stop_limit_buffer_pct,
            entries_allowed=True,  # the ONLY place: assert_trade_only just passed above
        )
        ctx = lifecycle.LiveContext(
            session=session, account=account, venue=venue, settings=settings, notifier=notifier, now=now, is_demo=is_demo,
            slot_cap=live_multi.slot_cap(account.live_slots, switch_on=settings.live_multi_position),
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
                    await _block_entry(notifier, account, settings, telemetry.R_FUTURES_NOT_ISOLATED, leverage_module.MSG_NOT_ISOLATED.format(pair=pair), now=now)
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
                                       f"your Binance Futures leverage on {pair} ({leverage:g}x) is above the limit of {settings.futures_max_leverage:g}x set for this service. "
                                       "Lower it for this pair on Binance. MBA Bot never changes it for you.", now=now)
                    return
                if margin_mode != "isolated":
                    _tlog(telemetry.REJECTED, telemetry.R_FUTURES_NOT_ISOLATED, margin_mode=margin_mode)
                    await _block_entry(notifier, account, settings, telemetry.R_FUTURES_NOT_ISOLATED,
                                       f"your Binance Futures margin mode on {pair} is not isolated (or could not be read), and this service trades isolated margin only. "
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
        if open_live:
            # The open risk across the trades is capped: every open trade at its CURRENT stop plus this one must stay within LIVE_TOTAL_RISK_CAP_PCT of the trading capital and within the daily dollar limit.
            refused = live_multi.check_additional_entry(
                open_live, new_risk=quantity * size.stop_distance, capital=account.allocated_capital, cap_pct=settings.live_total_risk_cap_pct,
                daily_limit_usd=account.daily_loss_limit_usd if settings.account_daily_loss_limit else None,
            )
            if refused is not None:
                _tlog(telemetry.REJECTED, refused, open_risk=round(live_multi.total_open_risk(open_live), 4), new_risk=round(quantity * size.stop_distance, 4), open_trades=len(open_live))
                if protection_guard.should_alert(account.id, pair, f"multi_risk:{refused}", now=now, repeat_hours=6.0):
                    logger.info("account %s: additional entry on %s refused (%s): open risk %.4f, new %.4f", account.id, pair, refused, live_multi.total_open_risk(open_live), quantity * size.stop_distance)
                return
        precheck_detail = None
        if account.market_type == "futures" and settings.protection_precheck_mode != "off":
            check = await _precheck_entry(
                client, venue, settings, side=side, entry_price=current_price, stop_price=stop_price, quantity=quantity,
                leverage=leverage, margin_mode=margin_mode, effective_capital=effective_capital, cache_key=f"{account.id}:{venue.symbol}",
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
                        account.id, side, pair, check.reason, precheck_detail,
                    )
                else:
                    _tlog(telemetry.REJECTED, check.reason, **detail)
                    logger.warning("account %s: protection pre-check refused (%s) %s", account.id, check.reason, precheck_detail)
                    await _block_entry(notifier, account, settings, check.reason, _precheck_message(account, check, leverage), now=now)
                    return
            elif settings.protection_precheck_mode == "observe" and protection_guard.should_alert(
                account.id, pair, "observe:pass", now=now, repeat_hours=_PASS_LOG_REPEAT_HOURS
            ):
                # Journal record of a PASS (the decision log carries it on the accepted row when telemetry is on), so
                # observe mode is measurable without telemetry. One line per entry, not per round.
                logger.info(
                    "account %s: [observe] protection pre-check passes %s %s at %gx: stop %.3f%% of entry, allowed "
                    "%.3f%%, bracket %s (effective maintenance rate %.3f%%), %s margin",
                    account.id, side, pair, leverage, check.stop_distance_frac * 100,
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
                risk_text=f"Risk: {risk_amount:.2f} ({risk_pct_used:.2f}% of balance)",
                strategy_id=strategy_id,
                pair=pair,
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
        initial_stop_price=stop_price,  # the R of the EMA exit rules (docs/safety/ema_exit_rules.md); nothing reads it unless one of them is on
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
    if not is_paper and live_multi.slot_cap(account.live_slots, switch_on=settings.live_multi_position) > 1:
        return True  # LIVE_MULTI_POSITION and the account has more than one slot
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


async def _process_multi(session, account, client, open_trades: list[Trade], settings, notifier, *, is_paper: bool, now, is_demo: bool = False) -> None:
    notifier = _SafeNotifier(notifier, account)  # one failed message must not stop the other trades being managed (audit L8)
    live = [t for t in open_trades if not t.is_paper]
    if (live or not is_paper) and live_multi.slot_cap(account.live_slots, switch_on=settings.live_multi_position) > 1:
        await _process_live_multi(session, account, client, live, settings, notifier, now=now, is_demo=is_demo)
        return
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
            _track_peak(session, t, await don_closed_for(pair), None, now_ms=now_ms)
            _paper_staged_ratchet(t, await don_closed_for(pair), None, settings, tf_ms=don_tf_ms, atr_period=don_p.atr_period, now_ms=now_ms, account=account)
            if await _paper_time_stop(session, account, t, await don_closed_for(pair), None, settings, notifier, tf_ms=don_tf_ms, atr_period=don_p.atr_period, now_ms=now_ms, limit=settings.donchian_max_hold_candles, now=now):
                continue
            await _manage_donchian_trade(session, account, t, await don_closed_for(pair), settings, notifier, p=don_p, tf_ms=don_tf_ms, now=now)
            continue
        if multi_trade.strategy_of(t) == rsi_mr.STRATEGY_ID:
            _track_peak(session, t, await rsi_closed_for(pair), None, now_ms=now_ms)
            _paper_staged_ratchet(t, await rsi_closed_for(pair), None, settings, tf_ms=rsi_tf_ms, atr_period=rsi_p.atr_period, now_ms=now_ms, account=account)
            await _manage_rsi_trade(session, account, t, await rsi_closed_for(pair), settings, notifier, p=rsi_p, tf_ms=rsi_tf_ms, now=now)
            continue
        candles = await candles_for(pair)
        if not candles:
            logger.warning("account %s: no OHLCV data returned for %s", account.id, pair)
            await _record_error(notifier, account, "No market data was returned for this account's pair just now.", now=now)
            continue
        _track_peak(session, t, candles, candles[-1].close, now_ms=int(now.timestamp() * 1000))
        _paper_staged_ratchet(t, rsi_mr.closed_only(candles, now_ms, rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(settings.timeframe)[0])), candles[-1].close, settings, tf_ms=rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(settings.timeframe)[0]), atr_period=settings.atr_period, now_ms=now_ms, account=account)
        _tfms = rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(settings.timeframe)[0])
        if await _paper_time_stop(session, account, t, rsi_mr.closed_only(candles, now_ms, _tfms), candles[-1].close, settings, notifier, tf_ms=_tfms, atr_period=settings.atr_period, now_ms=now_ms, limit=settings.ema_max_hold_candles, now=now):
            continue
        _rev = _reversal_reason(t, rsi_mr.closed_only(candles, now_ms, _tfms), settings, now_ms, candles[-1].close)
        if _rev is not None:
            await _paper_close_at_price(session, account, t, candles[-1].close, _rev, "EMA reversal", settings, notifier, now=now)
            continue
        await _maybe_exit(session, account, client, t, signal_for(candles), candles[-1].close, notifier, is_paper=True, now=now,
                          settings=settings, multi_label=True, take_profit=_paper_ema_rules(t, candles, settings, int(now.timestamp() * 1000)))

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
                opportunity.record_not_taken(session, settings, account, strategy_id=strategy, pair=pair, candle_ts=closed[-1].timestamp, reason=opportunity.R_STALE, is_paper=True, now=now,
                                             detail={"age_minutes": round(age_ms / 60_000, 1), "window_minutes": settings.new_strategy_entry_window_minutes})  # observability only
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
                    if closed:
                        opportunity.record_verdict(session, settings, account, strategy_id=strategy, pair=pair, candle_ts=closed[-1].timestamp, sig=sig, price=closed[-1].close, is_paper=True, now=now, tf=don_tf)  # observability only
                    return (sig, closed[-1].close, closed) if (sig.side and closed and fresh(strategy, pair, closed, don_tf_ms)) else None
                if strategy == rsi_mr.STRATEGY_ID:
                    closed = await rsi_closed_for(pair)
                    sig = rsi_mr.generate_signal(closed, rsi_p, allow_short=allow_short)
                    if closed:
                        opportunity.record_verdict(session, settings, account, strategy_id=strategy, pair=pair, candle_ts=closed[-1].timestamp, sig=sig, price=closed[-1].close, is_paper=True, now=now, tf=rsi_tf)  # observability only
                    return (sig, closed[-1].close, closed) if (sig.side and closed and fresh(strategy, pair, closed, rsi_tf_ms)) else None
                candles = await candles_for(pair)
                if not candles:
                    return None
                sig = signal_for(candles)
                ema_closed = _ema_signal_candles(candles, settings, now_ms)  # the list the signal was made from (the raw one with the switch off)
                if ema_closed:
                    opportunity.record_verdict(session, settings, account, strategy_id=strategy, pair=pair, candle_ts=ema_closed[-1].timestamp, sig=sig, price=ema_closed[-1].close, is_paper=True, now=now,
                                               tf=settings.timeframe)  # observability only
                if sig.signal == SignalType.NONE:
                    return None
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


async def _live_donchian_entry(session, account, client, settings: Settings, notifier, *, now, now_ms: int, is_demo: bool) -> None:
    """LIVE_DONCHIAN only (default off). Looks for a Donchian breakout entry on the account's pair from CLOSED candles, only if the account is opted in (extra_strategies). The entry goes through
    `_maybe_enter` exactly like an EMA entry (kill switch, daily dollar limit, 50% total stop, guards in enforce and the fee guard, pre-entry protection check, leverage cap, risk per trade, the protective
    stop placed and verified right after the fill). Spot is long only."""
    if not settings.donchian_enabled or donchian.STRATEGY_ID not in multi_trade.account_strategies(account):
        return
    if list_open_trades(session, account.id):
        return
    tf, _ = rsi_mr.normalize_timeframe(settings.donchian_timeframe)
    tf_ms = rsi_mr.timeframe_ms(tf)
    ohlcv = await client.fetch_ohlcv(exchange.exchange_symbol(account.pair, account.market_type), timeframe=tf, limit=settings.ohlcv_limit)
    raw = candles_from_ohlcv(ohlcv)
    closed = rsi_mr.closed_only(raw, now_ms, tf_ms)
    if not raw or not closed:
        return
    sig = donchian.signal_from_candles(closed, _donchian_params(settings), allow_short=account.market_type == "futures")
    opportunity.record_verdict(session, settings, account, strategy_id=donchian.STRATEGY_ID, pair=account.pair, candle_ts=closed[-1].timestamp, sig=sig, price=closed[-1].close, is_paper=False, now=now, tf=tf)  # observability only
    if not sig.side:
        return
    age_ms = now_ms - (closed[-1].timestamp + tf_ms)
    window_ms = int(settings.new_strategy_entry_window_minutes * 60_000)
    if not 0 <= age_ms <= window_ms:
        opportunity.record_not_taken(session, settings, account, strategy_id=donchian.STRATEGY_ID, pair=account.pair, candle_ts=closed[-1].timestamp, reason=opportunity.R_STALE, is_paper=False, now=now,
                                     detail={"age_minutes": round(age_ms / 60_000, 1), "window_minutes": settings.new_strategy_entry_window_minutes})
        if protection_guard.should_alert(account.id, account.pair, f"stale:{donchian.STRATEGY_ID}:{closed[-1].timestamp}", now=now, repeat_hours=24.0):
            logger.info("account %s: donchian_breakout_v1 signal on %s skipped (signal_stale): closed %.0f minutes ago, window %g", account.id, account.pair, age_ms / 60_000, settings.new_strategy_entry_window_minutes)
        return
    await _maybe_enter(session, account, client, sig, raw[-1].close, settings, notifier, is_paper=False, now=now, is_demo=is_demo)


async def _live_donchian_manage(ctx, trade: Trade, client, settings: Settings, *, now_ms: int, current_price: float) -> None:
    """The strategy's own management (below), then the staged profit lock (Part C, off by default) on the same candles."""
    seen: list = []
    await _live_donchian_rules(ctx, trade, client, settings, now_ms=now_ms, current_price=current_price, seen=seen)
    if seen and trade.status == TRADE_OPEN:
        p = _donchian_params(settings)
        tf, _ = rsi_mr.normalize_timeframe(settings.donchian_timeframe)
        await _live_staged_lock(ctx, trade, settings, closed=seen[0], tf_ms=rsi_mr.timeframe_ms(tf), atr_period=p.atr_period, price=current_price, now_ms=now_ms)
        await _live_time_stop(ctx, trade, settings, closed=seen[0], tf_ms=rsi_mr.timeframe_ms(tf), atr_period=p.atr_period, price=current_price, now_ms=now_ms, limit=settings.donchian_max_hold_candles)


async def _live_donchian_rules(ctx, trade: Trade, client, settings: Settings, *, now_ms: int, current_price: float, seen: list) -> None:
    """A live donchian_breakout_v1 trade: (1) a CLOSE beyond the opposite 10-candle channel exits through `lifecycle.execute_exit` (the only live exit path); (2) otherwise the protective stop is trailed
    (`lifecycle.move_stop`: favourable direction only, new stop placed first, old one cancelled after, a failed move keeps the old stop). The initial stop and the trailing stop are exchange-side
    stops; touching either inside the candles is left to the exchange and the software backstop. Runs whenever such a trade is open, whatever the switch says."""
    tf, _ = rsi_mr.normalize_timeframe(settings.donchian_timeframe)
    tf_ms = rsi_mr.timeframe_ms(tf)
    trade_pair = trade.pair or ctx.account.pair
    ohlcv = await client.fetch_ohlcv(exchange.exchange_symbol(trade_pair, ctx.account.market_type), timeframe=tf, limit=settings.ohlcv_limit)
    closed = rsi_mr.closed_only(candles_from_ohlcv(ohlcv), now_ms, tf_ms)
    if not closed:
        return
    seen.append(closed)
    p = _donchian_params(settings)
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    opened_ms = int(opened.timestamp() * 1000)
    initial = trade.initial_stop_price if trade.initial_stop_price else trade.stop_loss_price
    ex = donchian.exit_check(side=trade.side, entry=trade.entry_price, stop=initial, opened_ms=opened_ms, closed=closed, tf_ms=tf_ms, p=p)
    if ex is not None and ex.reason == donchian.EXIT_CHANNEL:
        await lifecycle.execute_exit(ctx, trade, reason=ex.reason, price_hint=ex.price)
        return
    if ex is not None:
        return  # the stop or the trailing stop was touched: the exchange-side stop owns that exit
    if settings.profit_every_round:
        # Part B: the Donchian trail from the RUNNING best price (same distance, trail_atr x the latest closed ATR), every round, with the call limits.
        atr_now = donchian_trail.latest_atr(closed, p)
        peak = profit_protect.advance_peak(trade.side, trade.peak_price, current_price) or trade.entry_price
        level_run = donchian_trail.running_level(side=trade.side, peak=peak, atr=atr_now, p=p)
        await profit_live.running_step(ctx, trade, settings, level=level_run, label=donchian.EXIT_TRAIL, price=current_price, atr=atr_now, now_ms=now_ms)
        return
    level = donchian_trail.trailing_level(side=trade.side, entry=trade.entry_price, opened_ms=opened_ms, closed=closed, p=p)
    if level is not None:
        await lifecycle.move_stop(ctx, trade, new_stop=level, current_price=current_price)


async def _live_rsi_entry(session, account, client, settings: Settings, notifier, *, now, now_ms: int, is_demo: bool) -> None:
    """LIVE_RSI_MR only (default off). Looks for an RSI mean-reversion entry on the account's pair from CLOSED candles, only if the account is opted in (extra_strategies) and EMA took no entry this
    cycle (one live slot, EMA first). The entry itself goes through `_maybe_enter` exactly like an EMA entry: kill switch, daily dollar limit and 50% total stop, strategy guards (enforce) and fee guard,
    pre-entry protection check, leverage cap, risk per trade (the smaller of the account's and RSI_MR_RISK_PCT), the protective stop placed and verified right after the fill."""
    if not settings.rsi_mr_enabled or rsi_mr.STRATEGY_ID not in multi_trade.account_strategies(account):
        return
    if list_open_trades(session, account.id):
        return  # a trade is open (EMA just opened one, or one was already running): one live slot
    tf, _ = rsi_mr.normalize_timeframe(settings.rsi_mr_timeframe)
    tf_ms = rsi_mr.timeframe_ms(tf)
    pair = account.pair
    ohlcv = await client.fetch_ohlcv(exchange.exchange_symbol(pair, account.market_type), timeframe=tf, limit=settings.ohlcv_limit)
    raw = candles_from_ohlcv(ohlcv)
    closed = rsi_mr.closed_only(raw, now_ms, tf_ms)
    if not raw or not closed:
        return
    sig = rsi_mr.generate_signal(closed, _rsi_params(settings), allow_short=account.market_type == "futures")
    opportunity.record_verdict(session, settings, account, strategy_id=rsi_mr.STRATEGY_ID, pair=pair, candle_ts=closed[-1].timestamp, sig=sig, price=closed[-1].close, is_paper=False, now=now, tf=tf)  # observability only
    if not sig.side:
        return
    age_ms = now_ms - (closed[-1].timestamp + tf_ms)
    window_ms = int(settings.new_strategy_entry_window_minutes * 60_000)
    if not 0 <= age_ms <= window_ms:  # the signal candle must be the latest closed one and closed within the window (audit M5)
        opportunity.record_not_taken(session, settings, account, strategy_id=rsi_mr.STRATEGY_ID, pair=pair, candle_ts=closed[-1].timestamp, reason=opportunity.R_STALE, is_paper=False, now=now,
                                     detail={"age_minutes": round(age_ms / 60_000, 1), "window_minutes": settings.new_strategy_entry_window_minutes})
        if protection_guard.should_alert(account.id, pair, f"stale:{rsi_mr.STRATEGY_ID}:{closed[-1].timestamp}", now=now, repeat_hours=24.0):
            logger.info("account %s: rsi_mr_v1 signal on %s skipped (signal_stale): closed %.0f minutes ago, window %g", account.id, pair, age_ms / 60_000, settings.new_strategy_entry_window_minutes)
        return
    await _maybe_enter(session, account, client, sig, raw[-1].close, settings, notifier, is_paper=False, now=now, is_demo=is_demo)


async def _live_rsi_exit(ctx, trade: Trade, client, settings: Settings, *, now_ms: int, current_price: float | None = None) -> None:
    """The strategy's own exits (below), then the staged profit lock (Part C, off by default) on the same candles."""
    seen: list = []
    await _live_rsi_rules(ctx, trade, client, settings, now_ms=now_ms, seen=seen)
    if seen and trade.status == TRADE_OPEN:
        tf, _ = rsi_mr.normalize_timeframe(settings.rsi_mr_timeframe)
        price = current_price if current_price is not None else seen[0][-1].close
        await _live_staged_lock(ctx, trade, settings, closed=seen[0], tf_ms=rsi_mr.timeframe_ms(tf), atr_period=_rsi_params(settings).atr_period, price=price, now_ms=now_ms)


async def _live_rsi_rules(ctx, trade: Trade, client, settings: Settings, *, now_ms: int, seen: list) -> None:
    """A live rsi_mr_v1 trade is exited by ITS rules (profit target, RSI back to 50, time stop) from the closed candles after it opened, through `lifecycle.execute_exit` (the only live exit path).
    The stop is the exchange-side stop placed at entry (and the software backstop in monitor_trade); a stop touch found here is left to them. Runs whenever such a trade is open, whatever the switch says."""
    tf, _ = rsi_mr.normalize_timeframe(settings.rsi_mr_timeframe)
    tf_ms = rsi_mr.timeframe_ms(tf)
    pair = trade.pair or ctx.account.pair
    ohlcv = await client.fetch_ohlcv(exchange.exchange_symbol(pair, ctx.account.market_type), timeframe=tf, limit=settings.ohlcv_limit)
    closed = rsi_mr.closed_only(candles_from_ohlcv(ohlcv), now_ms, tf_ms)
    if not closed:
        return
    seen.append(closed)
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    ex = rsi_mr.exit_check(side=trade.side, entry=trade.entry_price, stop=trade.stop_loss_price, opened_ms=int(opened.timestamp() * 1000), closed=closed, tf_ms=tf_ms, p=_rsi_params(settings))
    if ex is None or ex.reason == rsi_mr.EXIT_STOP:
        return
    await lifecycle.execute_exit(ctx, trade, reason=ex.reason, price_hint=ex.price)


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


async def _live_strategy_signal(client, account, pair: str, strategy: str, settings: Settings, *, now, now_ms: int, session, candles: list | None = None, meta: dict | None = None):
    """(signal, price) for one strategy on one pair from closed candles, or None when it has no actionable, fresh signal. Read-only. `candles` (the Auto Select scan's candles for this pair this round) is used instead
    of a new fetch when the strategy's timeframe is the scan's; with None, or another timeframe, the candles are fetched as before."""
    allow_short = account.market_type == "futures"
    symbol = exchange.exchange_symbol(pair, account.market_type)
    window_ms = int(settings.new_strategy_entry_window_minutes * 60_000)

    def fresh(closed: list, tf_ms: int, label: str) -> bool:
        age_ms = now_ms - (closed[-1].timestamp + tf_ms)
        if 0 <= age_ms <= window_ms:
            return True
        opportunity.record_not_taken(session, settings, account, strategy_id=label, pair=pair, candle_ts=closed[-1].timestamp, reason=opportunity.R_STALE, is_paper=False, now=now,
                                     detail={"age_minutes": round(age_ms / 60_000, 1), "window_minutes": settings.new_strategy_entry_window_minutes})  # observability only
        if protection_guard.should_alert(account.id, pair, f"stale:{label}:{closed[-1].timestamp}", now=now, repeat_hours=24.0):
            logger.info("account %s: %s signal on %s skipped (signal_stale): closed %.0f minutes ago, window %g", account.id, label, pair, age_ms / 60_000, settings.new_strategy_entry_window_minutes)
        return False

    if strategy == donchian.STRATEGY_ID:
        tf, _ = rsi_mr.normalize_timeframe(settings.donchian_timeframe)
        tf_ms = rsi_mr.timeframe_ms(tf)
        raw = candles if (candles is not None and tf == rsi_mr.normalize_timeframe(settings.timeframe)[0]) else candles_from_ohlcv(await client.fetch_ohlcv(symbol, timeframe=tf, limit=settings.ohlcv_limit))
        closed = rsi_mr.closed_only(raw, now_ms, tf_ms)
        if not raw or not closed:
            return None
        sig = donchian.signal_from_candles(closed, _donchian_params(settings), allow_short=allow_short)
        opportunity.record_verdict(session, settings, account, strategy_id=strategy, pair=pair, candle_ts=closed[-1].timestamp, sig=sig, price=closed[-1].close, is_paper=False, now=now, tf=tf)  # observability only
        if meta is not None:
            meta["candle_ts"] = closed[-1].timestamp  # observability only: which candle the verdict is about
        return (sig, raw[-1].close) if (sig.side and fresh(closed, tf_ms, strategy)) else None
    if strategy == rsi_mr.STRATEGY_ID:
        tf, _ = rsi_mr.normalize_timeframe(settings.rsi_mr_timeframe)
        tf_ms = rsi_mr.timeframe_ms(tf)
        raw = candles if (candles is not None and tf == rsi_mr.normalize_timeframe(settings.timeframe)[0]) else candles_from_ohlcv(await client.fetch_ohlcv(symbol, timeframe=tf, limit=settings.ohlcv_limit))
        closed = rsi_mr.closed_only(raw, now_ms, tf_ms)
        if not raw or not closed:
            return None
        sig = rsi_mr.generate_signal(closed, _rsi_params(settings), allow_short=allow_short)
        opportunity.record_verdict(session, settings, account, strategy_id=strategy, pair=pair, candle_ts=closed[-1].timestamp, sig=sig, price=closed[-1].close, is_paper=False, now=now, tf=tf)  # observability only
        if meta is not None:
            meta["candle_ts"] = closed[-1].timestamp  # observability only
        return (sig, raw[-1].close) if (sig.side and fresh(closed, tf_ms, strategy)) else None
    candles = candles_from_ohlcv(await client.fetch_ohlcv(symbol, timeframe=settings.timeframe, limit=settings.ohlcv_limit))
    if not candles:
        return None
    sig_candles = _ema_signal_candles(candles, settings, now_ms)
    sig = _ema_generate(sig_candles, settings)
    if sig_candles:
        opportunity.record_verdict(session, settings, account, strategy_id=multi_trade.EMA_STRATEGY_ID, pair=pair, candle_ts=sig_candles[-1].timestamp, sig=sig, price=sig_candles[-1].close, is_paper=False, now=now,
                                   tf=settings.timeframe)  # observability only
    if sig.signal == SignalType.NONE or _ema_entry_stale(session, settings, account, sig, sig_candles, pair, now=now, now_ms=now_ms, is_paper=False):
        return None
    return sig, candles[-1].close


async def _process_live_multi(session, account, client, live_trades: list[Trade], settings, notifier, *, now, is_demo: bool) -> None:
    """LIVE_MULTI_POSITION and the account has more than one slot (docs/safety/live_multi_position.md). (1) Every open live trade is reconciled against Binance and managed with ITS strategy's rules
    (EMA: opposite crossover; RSI: target, RSI line, time stop; Donchian: channel exit and the trailing stop), whatever the strategy switches say. (2) At most ONE new entry per cycle, only if a slot is
    free, on a pair with no open trade, in the priority Donchian, then EMA, then RSI. Every entry goes through `_maybe_enter` (kill switch, daily dollar limit, 50% total stop, guards, pre-entry
    protection check, leverage cap, risk per trade, protective stop) plus the open-risk cap and the shared capital for a second or third trade."""
    now_ms = int(now.timestamp() * 1000)
    for t in live_trades:
        t, ctx = await _manage_live_trade(session, account, client, t, settings, notifier, now=now, is_demo=is_demo)
        if t.status != TRADE_OPEN or t.protection_status != PROTECTION_PROTECTED:
            continue  # closed this cycle, or unhealthy and worked by the reconciler: no strategy decision on unverified protection
        pair = t.pair or account.pair
        strategy = multi_trade.strategy_of(t)
        ohlcv = await client.fetch_ohlcv(exchange.exchange_symbol(pair, account.market_type), timeframe=settings.timeframe, limit=settings.ohlcv_limit)
        candles = candles_from_ohlcv(ohlcv)
        if not candles:
            logger.warning("account %s: no OHLCV data returned for %s", account.id, pair)
            continue
        price = candles[-1].close
        _track_peak(session, t, candles, price, now_ms=now_ms)  # the best price since entry (observation only)
        await _giveback_alert(session, account, t, price, settings, now=now)
        if strategy == donchian.STRATEGY_ID:
            await lifecycle.monitor_trade(ctx, t, crossover_exit=False, current_price=price)
            if t.status == TRADE_OPEN:
                await _live_donchian_manage(ctx, t, client, settings, now_ms=now_ms, current_price=price)
        elif strategy == rsi_mr.STRATEGY_ID:
            await lifecycle.monitor_trade(ctx, t, crossover_exit=False, current_price=price)
            if t.status == TRADE_OPEN:
                await _live_rsi_exit(ctx, t, client, settings, now_ms=now_ms, current_price=price)
        else:
            signal = _ema_generate(_ema_signal_candles(candles, settings, now_ms), settings)
            opposite = (t.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (t.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER)
            await lifecycle.monitor_trade(ctx, t, crossover_exit=opposite, current_price=price)
            if t.status == TRADE_OPEN:
                await _live_ema_manage(ctx, t, settings, now_ms=now_ms, candles=candles, current_price=price)
    account.last_error = None
    account.last_error_at = None

    slots = live_multi.slot_cap(account.live_slots, switch_on=settings.live_multi_position)
    open_now = [t for t in list_open_trades(session, account.id) if not t.is_paper]
    if len(open_now) < slots:
        taken = {t.pair or account.pair for t in open_now}
        free_pairs = [p for p in live_multi.live_pairs(account) if p not in taken]
        opted = multi_trade.account_strategies(account)
        order = (
            ([donchian.STRATEGY_ID] if (settings.live_donchian and settings.donchian_enabled and donchian.STRATEGY_ID in opted) else [])
            + [multi_trade.EMA_STRATEGY_ID]
            + ([rsi_mr.STRATEGY_ID] if (settings.live_rsi_mr and settings.rsi_mr_enabled and rsi_mr.STRATEGY_ID in opted) else [])
        )
        entered = False
        for strategy in order:
            for pair in free_pairs:
                got = await _live_strategy_signal(client, account, pair, strategy, settings, now=now, now_ms=now_ms, session=session)
                if got is None:
                    continue
                sig, price = got
                if strategy == multi_trade.EMA_STRATEGY_ID:
                    account.last_signal = sig.signal.value
                    account.last_signal_at = now
                await _maybe_enter(session, account, client, sig, price, settings, notifier, is_paper=False, now=now, pair=pair, is_demo=is_demo)
                if len([t for t in list_open_trades(session, account.id) if not t.is_paper]) > len(open_now):
                    entered = True
                    break  # one new entry per cycle
            if entered:
                break
    await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)


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
        _track_peak(session, t, candles, candles[-1].close, now_ms=int(now.timestamp() * 1000))
        signal = _ema_generate(_ema_signal_candles(candles, settings, int(now.timestamp() * 1000)), settings)
        opposite = (t.side == "long" and signal.signal == SignalType.BEARISH_CROSSOVER) or (t.side == "short" and signal.signal == SignalType.BULLISH_CROSSOVER)
        await lifecycle.monitor_trade(ctx, t, crossover_exit=opposite, current_price=candles[-1].close)
    account.last_error = None
    account.last_error_at = None
    await _maybe_send_daily_summary(session, account, notifier, now=now, settings=settings)


def _track_peak(session, trade: Trade, candles: list, price: float | None, *, now_ms: int) -> None:
    """Record the best price reached since the entry on `trade` (live or paper, any strategy): from the live price and the highs and lows of the candles that started after the entry. It only moves in the
    favourable direction. It is observation only (docs/safety/profit_peak_tracking.md): it never decides an entry or an exit by itself, it never raises into the cycle, and a failed write is rolled back and logged."""
    try:
        if trade.status not in (TRADE_OPEN, "pending", "closing", "attention") or not trade.entry_price:
            return
        opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
        new = profit_protect.peak_from_candles(trade.side, trade.entry_price, int(opened.timestamp() * 1000), candles, price, trade.peak_price)
        if trade.peak_price is None or profit_protect.is_better(trade.side, new, trade.peak_price):
            trade.peak_price = new
            session.commit()
    except Exception:  # noqa: BLE001 - an observation must never stop trade management
        logger.exception("trade %s: could not record the peak price", getattr(trade, "id", "?"))
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass


async def _giveback_alert(session, account, trade: Trade, price: float | None, settings: Settings, *, now) -> None:
    """ONE owner alert per open LIVE trade that has given back GIVEBACK_ALERT_FRACTION of the profit at its best price, once that best price was at least GIVEBACK_ALERT_MIN_PEAK_R x R (Part G,
    docs/safety/give_back_alarm.md). Off at 0. Alert only: no order, no stop move, no change to anything. Claimed once per trade in the database (alert_marks), so a restart or a second process does not repeat it. Text
    holds the bot code, the pair and numbers, never a name, an email or a key. Never raises."""
    try:
        frac = settings.giveback_alert_fraction
        if frac <= 0 or trade.is_paper or trade.status != TRADE_OPEN or not trade.initial_stop_price or trade.peak_price is None or price is None:
            return
        r = abs(trade.entry_price - trade.initial_stop_price)
        if r <= 0:
            return
        long_side = trade.side == "long"
        peak_gain = (trade.peak_price - trade.entry_price) if long_side else (trade.entry_price - trade.peak_price)
        now_gain = (price - trade.entry_price) if long_side else (trade.entry_price - price)
        if peak_gain < settings.giveback_alert_min_peak_r * r or peak_gain <= 0 or (peak_gain - now_gain) / peak_gain < frac:
            return
        if not protection_guard.claim_alert(session, account.id, trade.pair or account.pair, f"giveback:{trade.id}", now=now, repeat_hours=24.0 * 365 * 10):
            return
        await strategy_guards.alert_owner(settings, f"Give-back: bot {account.bot_code or 'unknown'} {trade.pair or account.pair} {trade.side}: best price was {peak_gain / r:+.1f} R, now {now_gain / r:+.1f} R "
                                          f"({(peak_gain - now_gain) / peak_gain * 100:.0f}% of the peak profit given back). Alert only, nothing was changed.")
    except Exception:  # noqa: BLE001 - an alert must never stop trade management
        logger.exception("trade %s: could not send the give-back alert", getattr(trade, "id", "?"))


def _paper_staged_ratchet(trade: Trade, closed: list, price: float | None, settings: Settings, *, tf_ms: int, atr_period: int, now_ms: int, account=None) -> None:
    """PAPER trades, staged profit lock on (Part C, off by default): ratchet the trade's stop to the level the running best price has earned, never loosened. If the price is already through the level the stop is set
    AT the price, so the paper exit is a market exit at the price, as the live trade's would be. The existing paper exit logic then fires on the stop. Nothing happens with every stage off."""
    rules = profit_protect.lock_rules_from_settings(settings)
    if not rules.any_on or not trade.initial_stop_price or not closed or not tf_ms:
        return
    price = price if price is not None else closed[-1].close
    atrs = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], atr_period)
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    held = max(0.0, now_ms - int(opened.timestamp() * 1000)) / tf_ms
    peak = profit_protect.advance_peak(trade.side, trade.peak_price, price) or trade.entry_price
    buf = profit_protect.cost_buffer(trade.entry_price, fee_bps_per_side=_fee_bps_for(account, settings), slippage_bps_round_trip=settings.fee_guard_slippage_bps)
    got = profit_protect.staged_level(side=trade.side, entry=trade.entry_price, initial_stop=trade.initial_stop_price, peak=peak, atr=atrs[-1] if atrs else None, held_candles=held, rules=rules, buffer=buf)
    if got is None:
        return
    target = min(got[0], price) if trade.side == "long" else max(got[0], price)
    if profit_protect.is_better(trade.side, target, trade.stop_loss_price):
        trade.stop_loss_price = target


async def _paper_time_stop(session, account, trade: Trade, closed: list, price: float | None, settings: Settings, notifier, *, tf_ms: int, atr_period: int, now_ms: int, limit: int, now) -> bool:
    """PAPER EMA and Donchian trades, maximum holding time on (Part D, off by default). At the limit: in profit net of costs the stop is ratcheted to the tightened level (never loosened; set at the price if the
    price is already through it, a market exit); flat or in loss the trade is closed at the price with the close reason time_stop. Returns True when the trade was closed here."""
    if limit <= 0 or not closed or not tf_ms or trade.status != TRADE_OPEN:
        return False
    price = price if price is not None else closed[-1].close
    atrs = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], atr_period)
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    held = max(0.0, now_ms - int(opened.timestamp() * 1000)) / tf_ms
    peak = profit_protect.advance_peak(trade.side, trade.peak_price, price) or trade.entry_price
    buf = profit_protect.cost_buffer(trade.entry_price, fee_bps_per_side=_fee_bps_for(account, settings), slippage_bps_round_trip=settings.fee_guard_slippage_bps)
    got = profit_protect.time_decision(side=trade.side, entry=trade.entry_price, price=price, peak=peak, atr=atrs[-1] if atrs else None, held_candles=held, limit_candles=limit, buffer=buf, tighten_atr=settings.profit_time_tighten_atr)
    if got is None:
        return False
    if got[0] == "tighten":
        target = min(got[1], price) if trade.side == "long" else max(got[1], price)
        if profit_protect.is_better(trade.side, target, trade.stop_loss_price):
            trade.stop_loss_price = target
        return False
    await _paper_close_at_price(session, account, trade, price, "time_stop", "time stop", settings, notifier, now=now)
    return True


async def _paper_close_at_price(session, account, trade: Trade, price: float, reason: str, label: str, settings: Settings, notifier, *, now) -> None:
    """Close a PAPER trade at `price` (a market exit) with a close reason, record the result and tell the customer. Paper only."""
    pnl = (price - trade.entry_price) * trade.quantity if trade.side == "long" else (trade.entry_price - price) * trade.quantity
    trade.exit_price, trade.pnl, trade.status, trade.closed_at, trade.close_reason = price, pnl, "closed", now, reason
    account.paper_balance += pnl
    session.commit()  # durable before the (network) notification
    pair = trade.pair or account.pair
    await notifier.send(account.notification_target, f"[PAPER] Exited {trade.side.upper()} {pair} ({label})\n" + pnl_labels.exit_line(trade.entry_price, price, pnl, on=bool(settings.pnl_labels), paper=True, quote=pnl_labels.quote_of(pair))
                        + f"\nStrategy: {multi_trade.strategy_of(trade)}")


def _ema_rule_inputs(trade: Trade, candles: list, settings: Settings, now_ms: int, tp_buffer: float = 0.0):
    """(rules, closed candles, ATR values, opened_ms) for an EMA trade's exit rules, or None when no rule is on, the trade is not an EMA trade, or it has no initial stop to measure R from."""
    rules = dataclasses.replace(ema_exits.rules_from_settings(settings), tp_buffer=max(0.0, tp_buffer))
    if not rules.any_on or multi_trade.strategy_of(trade) != multi_trade.EMA_STRATEGY_ID or not trade.initial_stop_price:
        return None
    closed = _ema_signal_candles(candles, settings, now_ms) if settings.ema_closed_candle_only else rsi_mr.closed_only(candles, now_ms, rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(settings.timeframe)[0]))
    if not closed:
        return None
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    atrs = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], settings.atr_period)
    return rules, closed, atrs, int(opened.timestamp() * 1000)


def _paper_tp_buffer(trade: Trade, settings: Settings) -> float:
    """The take-profit cost buffer for a PAPER trade: both fees at the ASSUMED rate plus the assumed slippage (a paper trade has no account tier to read). Zero when take profit is off."""
    if settings.ema_take_profit_r <= 0:
        return 0.0
    return profit_protect.cost_buffer(trade.entry_price, fee_bps_per_side=_fee_bps_for(None, settings), slippage_bps_round_trip=settings.fee_guard_slippage_bps)


def _paper_ema_rules(trade: Trade, candles: list, settings: Settings, now_ms: int) -> float | None:
    """PAPER EMA trades, rules on: ratchet the trade's stop to the level the closed candles have earned (never loosened) and return the take-profit price, or None. Nothing happens with every rule off."""
    got = _ema_rule_inputs(trade, candles, settings, now_ms, tp_buffer=_paper_tp_buffer(trade, settings))
    if got is None:
        return None
    rules, closed, atrs, opened_ms = got
    level = ema_exits.earned_stop(trade.side, trade.entry_price, trade.initial_stop_price, opened_ms, closed, rules, atrs)
    if level is not None and ((trade.side == "long" and level > trade.stop_loss_price) or (trade.side == "short" and level < trade.stop_loss_price)):
        trade.stop_loss_price = level
    return ema_exits.take_profit_level(trade.side, trade.entry_price, trade.initial_stop_price, rules.take_profit_r, rules.tp_buffer)


async def _live_ema_manage(ctx, trade: Trade, settings: Settings, *, now_ms: int, candles: list, current_price: float) -> None:
    """EMA exit rules (below), then the staged profit lock (Part C, off by default) on the same candles."""
    await _live_ema_rules(ctx, trade, settings, now_ms=now_ms, candles=candles, current_price=current_price)
    if trade.status == TRADE_OPEN:
        tf, _ = rsi_mr.normalize_timeframe(settings.timeframe)
        closed = rsi_mr.closed_only(candles, now_ms, rsi_mr.timeframe_ms(tf))
        await _live_staged_lock(ctx, trade, settings, closed=closed, tf_ms=rsi_mr.timeframe_ms(tf), atr_period=settings.atr_period, price=current_price, now_ms=now_ms)
        await _live_time_stop(ctx, trade, settings, closed=closed, tf_ms=rsi_mr.timeframe_ms(tf), atr_period=settings.atr_period, price=current_price, now_ms=now_ms, limit=settings.ema_max_hold_candles)
        await _live_ema_reversal(ctx, trade, settings, closed=closed, now_ms=now_ms, price=current_price)


def _fee_bps_for(account, settings: Settings) -> float:
    """The ASSUMED taker fee per side in basis points (FEE_GUARD_FEE_BPS, 5 by default): used where no exchange is at hand (paper trades) and as the fallback when the real tier cannot be read."""
    return settings.fee_guard_fee_bps


async def _live_cost_buffer(ctx, trade: Trade, settings: Settings, *, with_funding: bool) -> float:
    """The price distance a stop must sit beyond the entry to be at or above zero NET (docs/safety/net_pnl.md): both fees at the account's REAL taker rate when it can be read (cached 24 hours), else the assumed
    FEE_GUARD_FEE_BPS; the assumed round-trip slippage; and, for a Futures trade, the funding paid so far per unit (re-read at most every 30 minutes; a failed read counts as what is stored). Never raises."""
    bps, _source = await fee_tier.taker_bps(ctx.venue, ctx.account.id, assumed_bps=_fee_bps_for(ctx.account, settings))
    funding_per_unit = 0.0
    if with_funding and trade.quantity > 0:
        funding_per_unit = (await lifecycle.refresh_funding(ctx, trade)) / trade.quantity
    return profit_protect.cost_buffer(trade.entry_price, fee_bps_per_side=bps, slippage_bps_round_trip=settings.fee_guard_slippage_bps, funding_paid_per_unit=funding_per_unit)


async def _live_staged_lock(ctx, trade: Trade, settings: Settings, *, closed: list, tf_ms: int, atr_period: int, price: float, now_ms: int) -> None:
    """The staged profit lock (docs/safety/profit_staged_lock.md) for a healthy, protected live trade of ANY strategy. Off unless a stage or the trail is set. The level comes from the running best price
    (trades.peak_price), is judged by profit_protect.decide (never loosened, exit when the price is through it, call limits) and acts only through lifecycle.move_stop and lifecycle.execute_exit."""
    rules = profit_protect.lock_rules_from_settings(settings)
    if not rules.any_on or trade.status != TRADE_OPEN or trade.protection_status != PROTECTION_PROTECTED or not trade.initial_stop_price or not closed or not tf_ms:
        return
    atrs = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], atr_period)
    atr = atrs[-1] if atrs else None
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    held = max(0.0, now_ms - int(opened.timestamp() * 1000)) / tf_ms
    peak = profit_protect.advance_peak(trade.side, trade.peak_price, price) or trade.entry_price
    r_now = abs(trade.entry_price - trade.initial_stop_price)
    gain_r = ((peak - trade.entry_price) if trade.side == "long" else (trade.entry_price - peak)) / r_now if r_now > 0 else 0.0
    buf = await _live_cost_buffer(ctx, trade, settings, with_funding=rules.stage1_r > 0 and gain_r >= rules.stage1_r)  # the reads only happen once stage 1 could act
    got = profit_protect.staged_level(side=trade.side, entry=trade.entry_price, initial_stop=trade.initial_stop_price, peak=peak, atr=atr, held_candles=held, rules=rules, buffer=buf)
    if got is not None:
        await profit_live.running_step(ctx, trade, settings, level=got[0], label=got[1], price=price, atr=atr, now_ms=now_ms)


def _reversal_reason(trade: Trade, closed: list, settings: Settings, now_ms: int, price: float | None = None) -> str | None:
    """The reversal exit reason for an open EMA trade from the CLOSED candles (Part E), or None. Only an EMA trade, only with a rule on and an initial stop to measure R from."""
    rules = ema_reversal.rules_from_settings(settings)
    if not rules.any_on or multi_trade.strategy_of(trade) != multi_trade.EMA_STRATEGY_ID or not trade.initial_stop_price or not closed:
        return None
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    peak = profit_protect.advance_peak(trade.side, trade.peak_price, price) or trade.entry_price
    return ema_reversal.evaluate(side=trade.side, entry=trade.entry_price, initial_stop=trade.initial_stop_price, opened_ms=int(opened.timestamp() * 1000), closed=closed,
                                 fast_period=settings.ema_fast_period, slow_period=settings.ema_slow_period, peak=peak, rules=rules)


async def _live_ema_reversal(ctx, trade: Trade, settings: Settings, *, closed: list, now_ms: int, price: float) -> None:
    """A healthy, protected LIVE EMA trade with a reversal rule on (Part E, off by default): a closed candle back across the fast EMA after the trade was in profit, or the fast/slow gap shrunk by the set fraction
    of its peak, exits through lifecycle.execute_exit, the only live exit path. Never raises beyond what execute_exit does."""
    if trade.status != TRADE_OPEN or trade.protection_status != PROTECTION_PROTECTED:
        return
    reason = _reversal_reason(trade, closed, settings, now_ms, price)
    if reason is not None:
        await lifecycle.execute_exit(ctx, trade, reason=reason, price_hint=price)


async def _live_time_stop(ctx, trade: Trade, settings: Settings, *, closed: list, tf_ms: int, atr_period: int, price: float, now_ms: int, limit: int) -> None:
    """The maximum holding time (Part D, docs/safety/profit_time_stop.md), for a healthy, protected live EMA or Donchian trade; off at 0. At the limit a trade that is in profit net of costs has its stop tightened
    (lifecycle.move_stop, through profit_protect.decide: never loosened, call limits, exit if the price is already through the level); a trade that is flat or in loss exits through lifecycle.execute_exit with the
    close reason time_stop."""
    if limit <= 0 or trade.status != TRADE_OPEN or trade.protection_status != PROTECTION_PROTECTED or not closed or not tf_ms:
        return
    atrs = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], atr_period)
    atr = atrs[-1] if atrs else None
    opened = trade.opened_at if trade.opened_at.tzinfo else trade.opened_at.replace(tzinfo=timezone.utc)
    held = max(0.0, now_ms - int(opened.timestamp() * 1000)) / tf_ms
    peak = profit_protect.advance_peak(trade.side, trade.peak_price, price) or trade.entry_price
    if held < limit:
        return
    buf = await _live_cost_buffer(ctx, trade, settings, with_funding=True)  # only read once the limit is reached
    got = profit_protect.time_decision(side=trade.side, entry=trade.entry_price, price=price, peak=peak, atr=atr, held_candles=held, limit_candles=limit, buffer=buf, tighten_atr=settings.profit_time_tighten_atr)
    if got is None:
        return
    if got[0] == "exit":
        await lifecycle.execute_exit(ctx, trade, reason="time_stop", price_hint=price)
        return
    await profit_live.running_step(ctx, trade, settings, level=got[1], label="time_stop", price=price, atr=atr, now_ms=now_ms)


async def _live_ema_rules(ctx, trade: Trade, settings: Settings, *, now_ms: int, candles: list, current_price: float) -> None:
    """A healthy, protected LIVE EMA trade with an exit rule on (EMA_TAKE_PROFIT_R, EMA_TRAIL_TRIGGER_R, EMA_BREAKEVEN_R; all off by default): docs/safety/ema_exit_rules.md.
    * Take profit is MONITORED each round (no exchange-side order): when a candle after the entry, the forming candle or the live price has reached the level, the trade exits through lifecycle.execute_exit, the only
      live exit path, at the market. If the bot was down or between rounds the touch is still found from the candles. The protective stop stays the protection meanwhile.
    * A trailing or break-even level the closed candles have earned is applied with lifecycle.move_stop (new stop placed and verified before the old one is cancelled; never loosened; never beyond the price; a failed
      move keeps the old stop). If a closed candle already went through the earned level, the trade exits through execute_exit with that reason.
    Never raises into the cycle's trading decisions beyond what execute_exit and move_stop themselves do."""
    if trade.status != TRADE_OPEN or trade.protection_status != PROTECTION_PROTECTED:
        return
    tp_buffer = await _live_cost_buffer(ctx, trade, settings, with_funding=False) if settings.ema_take_profit_r > 0 else 0.0  # the take-profit target is net of costs at the account's real fee tier
    got = _ema_rule_inputs(trade, candles, settings, now_ms, tp_buffer=tp_buffer)
    if got is None:
        return
    rules, closed, atrs, opened_ms = got
    initial = trade.initial_stop_price
    w = ema_exits.walk(trade.side, trade.entry_price, initial, opened_ms, closed, rules, atrs)
    tp = ema_exits.take_profit_level(trade.side, trade.entry_price, initial, rules.take_profit_r, rules.tp_buffer)
    forming = [c for c in candles if c.timestamp >= opened_ms and c not in closed]  # the candle still forming (never the one the entry happened in: its start is before the entry)
    touched_now = ema_exits.take_profit_touched(trade.side, tp, current_price) or any(ema_exits.take_profit_touched(trade.side, tp, c.high if trade.side == "long" else c.low) for c in forming)
    if w.exit_reason == "stop_loss":
        return  # the initial stop was touched: the exchange-side stop and monitor_trade own that exit
    if w.exit_reason == "take_profit" or (w.exit_reason is None and touched_now):
        await lifecycle.execute_exit(ctx, trade, reason="take_profit", price_hint=tp)
        return
    if w.exit_reason in ("trailing_stop", "break_even_stop"):
        await lifecycle.execute_exit(ctx, trade, reason=w.exit_reason, price_hint=w.exit_price)
        return
    if settings.profit_every_round:
        # Part B: the level the RUNNING best price has earned, every round (peak is refreshed before this runs); one decision with the call limits.
        peak = profit_protect.advance_peak(trade.side, trade.peak_price, current_price) or trade.entry_price
        got = ema_exits.running_level(trade.side, trade.entry_price, initial, peak, rules, atrs[-1] if atrs else None)
        if got is not None:
            await profit_live.running_step(ctx, trade, settings, level=got[0], label=got[1], price=current_price, atr=atrs[-1] if atrs else None, now_ms=now_ms)
        return
    level = ema_exits.earned_stop(trade.side, trade.entry_price, initial, opened_ms, closed, rules, atrs)
    if level is not None:
        await lifecycle.move_stop(ctx, trade, new_stop=level, current_price=current_price)


async def _maybe_exit(session, account, client, open_trade: Trade, signal, current_price, notifier, *, is_paper, now, settings=None, multi_label=False, take_profit: float | None = None):
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
    hit_tp = (not hit_stop) and ema_exits.take_profit_touched(open_trade.side, take_profit, current_price)  # the stop wins if both are true; off (None) unless EMA_TAKE_PROFIT_R is set
    if not (hit_stop or opposite_cross or hit_tp):
        return

    exit_price = open_trade.stop_loss_price if hit_stop else (take_profit if hit_tp else current_price)

    if open_trade.side == "long":
        pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
    else:
        pnl = (open_trade.entry_price - exit_price) * open_trade.quantity

    open_trade.exit_price = exit_price
    open_trade.pnl = pnl
    open_trade.status = "closed"
    open_trade.closed_at = now
    if hit_stop:
        moved = open_trade.initial_stop_price is not None and open_trade.stop_loss_price != open_trade.initial_stop_price
        open_trade.close_reason = ("break_even_stop" if abs(open_trade.stop_loss_price - open_trade.entry_price) < 1e-9 else "trailing_stop") if moved else "stop_loss"
    else:
        open_trade.close_reason = "take_profit" if hit_tp else "crossover_exit"

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
    # OPPORTUNITY_LEDGER on: the same message gets a few more text lines (entry checks of the last 24 hours). Off, or nothing to say: the text is exactly what it was. No new message, no new alert type.
    lines = opportunity.summary_lines(session, account.id, now) if (settings is not None and opportunity.enabled(settings)) else []
    extra = ("\n" + "\n".join(lines)) if lines else ""
    if settings is not None and settings.reporting_v2:
        await notifier.send(account.notification_target, reporting.daily_summary_text(today, closed_today, quote) + extra)  # modes kept apart; "net" only with every fee known
        return
    await notifier.send(
        account.notification_target,
        pnl_labels.daily_summary(
            today, len(closed_today), total_pnl, quote, on=bool(settings and settings.pnl_labels),
            paper_count=sum(1 for t in closed_today if t.is_paper),
        ) + extra,
    )
