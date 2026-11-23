"""Opportunity ledger (docs/safety/opportunity_ledger.md). OBSERVABILITY ONLY: it records what each strategy concluded on each closed candle and why a signal was not taken. It never changes an entry decision.

Behind OPPORTUNITY_LEDGER (default off). With it on, the trader calls the functions below next to its existing strategy calls. Every function here:
  * never raises and never returns anything the trader uses (a failure is swallowed, so it cannot alter a decision);
  * only queues rows with `session.add` (no flush, no commit: the cycle commits as it always did), and checks for an existing row with `no_autoflush`;
  * writes one row per (account, strategy, pair, closed candle, outcome, reason), checked in memory and, once per key per process, in the database (so a restart does not repeat a row);
  * stops at OPPORTUNITY_LEDGER_DAILY_CAP rows per account and day, and deletes rows older than OPPORTUNITY_LEDGER_KEEP_DAYS (at most 5000 a day, first write of each UTC day);
  * stores numbers and fixed reason codes only: no key, no balance, no e-mail.

Rows use the existing decision_log table: stage "opportunity", outcome "no_signal" (the strategy found nothing), "signal" (it found an entry signal on this candle), or "not_taken" (a reason a signal or
a strategy was not acted on: signal_stale, not_best_ranked, strategy_not_evaluated). What happens to a signal after that (entry opened, or refused by a guard) is in the existing entry-stage rows."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from . import telemetry
from .models import DecisionLog

logger = logging.getLogger(__name__)

STAGE = "opportunity"
OUT_NO_SIGNAL, OUT_SIGNAL, OUT_NOT_TAKEN = "no_signal", "signal", "not_taken"

EMA_ID, DONCHIAN_ID, RSI_ID = "ema_cross_atr_v1", "donchian_breakout_v1", "rsi_mr_v1"
LABEL = {EMA_ID: "EMA crossover", DONCHIAN_ID: "Donchian breakout", RSI_ID: "RSI mean reversion"}

# Reason codes. The strategies' own reason strings (strategy.py, donchian.py, rsi_mr.py) are mapped here; tests/test_opportunity_ledger.py fails if a strategy gains a reason string that is not mapped.
R_STALE = "signal_stale"
R_NOT_BEST = "not_best_ranked"
R_NOT_EVALUATED = "strategy_not_evaluated"
R_CAPITAL_SKIP = "auto_skip_insufficient_capital"
REASON_BY_STRATEGY_TEXT = {
    EMA_ID: {
        "insufficient candle history": "ema_insufficient_history",
        "no crossover": "ema_no_cross",
        "flat/range-bound market: ATR% below rolling threshold": "ema_flat_market_filter",
    },
    DONCHIAN_ID: {
        "insufficient candle history": "donchian_insufficient_history",
        "indicator not available": "donchian_indicator_unavailable",
        "no breakout": "donchian_inside_channel",
        "wick beyond the channel, close inside: not a breakout": "donchian_wick_close_inside",
        "short breakout on a long-only market": "donchian_short_on_spot",
        "average volume not available": "donchian_volume_unavailable",
        "breakout without volume confirmation": "donchian_no_volume_confirmation",
        "ATR% already extreme: skipped": "donchian_atr_extreme",
    },
    RSI_ID: {
        "insufficient candle history": "rsi_insufficient_history",
        "indicator not available": "rsi_indicator_unavailable",
        "no RSI cross": "rsi_no_cross",
        "trending market: ADX not below the limit": "rsi_trending_market_adx",
        "short signal on a long-only market": "rsi_short_on_spot",
    },
}
SIGNAL_CODES = {EMA_ID: ("ema_cross_up", "ema_cross_down"), DONCHIAN_ID: ("donchian_breakout_long", "donchian_breakout_short"), RSI_ID: ("rsi_cross_up", "rsi_cross_down")}

# Plain words for the daily summary and the report (no jargon, no advice).
PLAIN = {
    "ema_insufficient_history": "not enough candles yet", "ema_no_cross": "no EMA crossover", "ema_flat_market_filter": "crossover ignored, market too flat",
    "donchian_insufficient_history": "not enough candles yet", "donchian_indicator_unavailable": "indicators not ready", "donchian_inside_channel": "price stayed inside the channel",
    "donchian_wick_close_inside": "price poked out of the channel but closed inside", "donchian_short_on_spot": "short breakout, not possible on Spot",
    "donchian_volume_unavailable": "volume not available", "donchian_no_volume_confirmation": "breakout without enough volume", "donchian_atr_extreme": "volatility already extreme",
    "rsi_insufficient_history": "not enough candles yet", "rsi_indicator_unavailable": "indicators not ready", "rsi_no_cross": "RSI did not cross back", "rsi_trending_market_adx": "market trending, RSI rule skipped",
    "rsi_short_on_spot": "short signal, not possible on Spot",
    "ema_cross_up": "EMA crossed up", "ema_cross_down": "EMA crossed down", "donchian_breakout_long": "breakout above the channel", "donchian_breakout_short": "breakout below the channel",
    "rsi_cross_up": "RSI crossed back up from oversold", "rsi_cross_down": "RSI crossed back down from overbought",
    R_STALE: "signal came too late to enter at a fair price", R_NOT_BEST: "another pair had a stronger signal", R_NOT_EVALUATED: "not checked this cycle", R_CAPITAL_SKIP: "pair too small for the trading capital",
    "maintenance_tiers_read_failed": "Binance's position-size table could not be read", "maintenance_tiers_invalid": "Binance's position-size table looked wrong",
    "maintenance_tiers_no_match": "position size outside Binance's table", "maintenance_tiers_unavailable": "Binance's position-size table was not available",
    "stop_beyond_safe_liquidation_distance": "stop too far for the leverage", "below_exchange_minimum_or_filters": "below Binance's minimum order",
    "fee_guard_expected_move_too_small": "expected move too small against costs", "kill_switch_engaged": "new trades paused by the operator", "account_daily_loss_pause": "daily loss limit reached",
    "account_total_loss_stop": "total loss stop active", "daily_loss_limit_missing": "no daily loss limit set", "futures_margin_not_isolated": "margin mode not isolated",
    "futures_leverage_above_ceiling": "leverage above your limit", "margin_mode_unknown": "margin mode unknown", "cross_margin_unverifiable": "cross margin with other positions",
    "protection_check_inputs_invalid": "safety check inputs invalid", "size_zero": "size rounded to zero", "size_zero_after_ceilings": "size rounded to zero after limits", "no_allocated_capital": "no trading capital set",
    "no_free_balance": "not enough free balance", "ema_gap_below_minimum": "EMA gap too small", "stop_loss_cooldown": "waiting after a stop-loss", "strategy_paused": "strategy paused",
    "strategy_loss_streak_pause": "strategy paused after losses", "strategy_expectancy_pause": "strategy paused (results)", "strategy_daily_loss_limit": "strategy daily loss limit",
    "portfolio_cap_reached": "portfolio limit reached", "strategy_guard_unreadable": "safety guard unreadable", "concurrent_trade_exists": "a trade was already open",
    "strategy_slot_lost_to": "another strategy took the slot", "protection_failure_limit": "repeated protection failures", "live_total_open_risk_cap": "open risk limit",
    "live_open_risk_above_daily_limit": "open risk above the daily limit", "live_free_capital_short": "no free capital for another trade",
}

_seen: dict[tuple, None] = {}
_SEEN_MAX = 20000
_day_count: dict[tuple[str, str], int] = {}
_capped_logged: set[tuple[str, str]] = set()
_pruned_day: list[str | None] = [None]
_PRUNE_BATCH = 5000


def reset_for_tests() -> None:
    _seen.clear()
    _day_count.clear()
    _capped_logged.clear()
    _pruned_day[0] = None


def enabled(settings) -> bool:
    return bool(getattr(settings, "opportunity_ledger", False)) and telemetry._schema_ready()


def _r(x):
    return None if x is None else round(float(x), 6)


def strength(strategy_id: str, sig) -> float | None:
    """How strong a Donchian or RSI signal was, as a plain number, for measuring the Auto Select tie-break (docs/safety/auto_select_strategies.md). It is NEVER used to choose anything.
    Donchian: how far the close is beyond the channel, in ATR units (long: close above the channel high; short: close below the channel low). RSI: how far the RSI is beyond its entry threshold, in RSI
    points (long: RSI minus the oversold line it crossed back above; short: the overbought line minus the RSI). None when the signal has no side or a needed number is missing."""
    try:
        side = getattr(sig, "side", None)
        if not side:
            return None
        if strategy_id == DONCHIAN_ID:
            if not sig.atr or sig.close is None:
                return None
            beyond = (sig.close - sig.upper) if side == "long" else (sig.lower - sig.close)
            return _r(beyond / sig.atr)
        if strategy_id == RSI_ID:
            if sig.rsi is None:
                return None
            from .rsi_mr import RsiParams  # the entry lines the strategy itself uses (the live code never overrides them)

            p = RsiParams()
            return _r((sig.rsi - p.oversold) if side == "long" else (p.overbought - sig.rsi))
    except Exception:  # noqa: BLE001 - a measurement must never break an evaluation
        return None
    return None


def verdict(strategy_id: str, sig) -> tuple[str, str, dict]:
    """(outcome, reason code, numbers) for one strategy result. Pure. The numbers are the ones that decided it."""
    if strategy_id == EMA_ID:
        kind = getattr(getattr(sig, "signal", None), "value", None)
        nums = {"fast": _r(sig.fast_ema), "slow": _r(sig.slow_ema), "gap_atr": _r(sig.gap_atr), "atr_pct": _r(sig.atr_pct)}
        if kind == "bullish_crossover":
            return OUT_SIGNAL, SIGNAL_CODES[EMA_ID][0], nums
        if kind == "bearish_crossover":
            return OUT_SIGNAL, SIGNAL_CODES[EMA_ID][1], nums
        return OUT_NO_SIGNAL, REASON_BY_STRATEGY_TEXT[EMA_ID].get(sig.reason, "ema_other"), nums
    if strategy_id == DONCHIAN_ID:
        vr = (sig.volume / sig.avg_volume) if (sig.volume is not None and sig.avg_volume) else None
        nums = {"channel_high": _r(sig.upper), "channel_low": _r(sig.lower), "volume_ratio": _r(vr), "atr_pct": _r(sig.atr_pct), "atr_pct_avg": _r(sig.atr_pct_avg)}
        if sig.side:
            return OUT_SIGNAL, SIGNAL_CODES[DONCHIAN_ID][0 if sig.side == "long" else 1], dict(nums, strength=strength(DONCHIAN_ID, sig))
        return OUT_NO_SIGNAL, REASON_BY_STRATEGY_TEXT[DONCHIAN_ID].get(sig.reason, "donchian_other"), nums
    nums = {"rsi": _r(sig.rsi), "prev_rsi": _r(sig.prev_rsi), "adx": _r(sig.adx), "atr_pct": _r(sig.atr_pct)}
    if sig.side:
        return OUT_SIGNAL, SIGNAL_CODES[RSI_ID][0 if sig.side == "long" else 1], dict(nums, strength=strength(RSI_ID, sig))
    return OUT_NO_SIGNAL, REASON_BY_STRATEGY_TEXT[RSI_ID].get(sig.reason, "rsi_other"), nums


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _prune(session, settings, now: datetime) -> None:
    """Retention: once per UTC day per process, delete up to _PRUNE_BATCH ledger rows older than the keep days (ledger rows only)."""
    day = _day(now)
    if _pruned_day[0] == day:
        return
    _pruned_day[0] = day
    try:
        cutoff = now.astimezone(timezone.utc) - timedelta(days=int(getattr(settings, "opportunity_ledger_keep_days", 30)))
        with session.no_autoflush:
            old = select(DecisionLog.id).where(DecisionLog.stage == STAGE, DecisionLog.ts < cutoff).limit(_PRUNE_BATCH)
            session.execute(delete(DecisionLog).where(DecisionLog.id.in_(old)))
    except Exception:  # noqa: BLE001
        logger.debug("opportunity ledger: retention pass failed; ignored")


def _write(session, settings, account, *, strategy_id, pair, candle_ts, outcome, reason, signal=None, price=None, detail=None, is_paper, now) -> None:
    try:
        if session is None or not enabled(settings):
            return
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        key = (account.id, strategy_id, pair, candle_ts, outcome, reason)
        if key in _seen:
            return
        day = _day(now)
        if _day_count.get((account.id, day), 0) >= int(getattr(settings, "opportunity_ledger_daily_cap", 1500)):
            if (account.id, day) not in _capped_logged:
                _capped_logged.add((account.id, day))
                logger.warning("opportunity ledger: the daily cap of %s rows is reached for account %s; no more rows today", settings.opportunity_ledger_daily_cap, account.id)
            return
        _prune(session, settings, now)
        if len(_seen) >= _SEEN_MAX:
            for old in list(_seen)[: _SEEN_MAX // 2]:
                _seen.pop(old, None)
        if len(_day_count) > 2000:
            _day_count.clear()
            _capped_logged.clear()
        _seen[key] = None
        if candle_ts is not None:  # the database check: a restart must not repeat a row (once per key per process)
            with session.no_autoflush:
                exists = session.execute(
                    select(DecisionLog.id).where(
                        DecisionLog.account_id == account.id, DecisionLog.stage == STAGE, DecisionLog.strategy_id == strategy_id, DecisionLog.pair == pair,
                        DecisionLog.candle_ts == candle_ts, DecisionLog.outcome == outcome, DecisionLog.reason_code == reason[:48],
                    ).limit(1)
                ).first()
            if exists is not None:
                return
        text = None
        if detail:
            text = json.dumps(detail, separators=(",", ":"), default=str, allow_nan=False)
            if len(text) > 1500:
                text = json.dumps({"truncated": True})
        session.add(DecisionLog(
            ts=now, account_id=account.id, mode="paper" if is_paper else "live", market_type=account.market_type, pair=pair, strategy_id=strategy_id, stage=STAGE, outcome=outcome,
            reason_code=reason[:48], signal=signal, price=price, candle_ts=candle_ts, detail_json=text,
        ))
        _day_count[(account.id, day)] = _day_count.get((account.id, day), 0) + 1
    except Exception as exc:  # noqa: BLE001 - never into the trading path
        logger.debug("opportunity ledger write failed (%s); ignored", type(exc).__name__)


def record_verdict(session, settings, account, *, strategy_id: str, pair: str, candle_ts, sig, price, is_paper: bool, now: datetime, tf: str | None = None) -> None:
    """One row for a strategy's verdict on one closed candle (no signal, or a signal)."""
    try:
        if not enabled(settings):
            return
        outcome, reason, nums = verdict(strategy_id, sig)
        detail = dict(nums, sel=getattr(account, "pair_selection_mode", None), tf=tf)
        side = getattr(sig, "side", None) or getattr(getattr(sig, "signal", None), "value", None)
        _write(session, settings, account, strategy_id=strategy_id, pair=pair, candle_ts=candle_ts, outcome=outcome, reason=reason, signal=(None if outcome == OUT_NO_SIGNAL else side),
               price=_r(price), detail={k: v for k, v in detail.items() if v is not None}, is_paper=is_paper, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.debug("opportunity ledger verdict failed (%s); ignored", type(exc).__name__)


def record_not_taken(session, settings, account, *, strategy_id: str, pair: str, candle_ts, reason: str, is_paper: bool, now: datetime, detail: dict | None = None) -> None:
    """One row for a reason a signal or a strategy was not acted on (signal_stale, not_best_ranked, strategy_not_evaluated, ...)."""
    try:
        if not enabled(settings):
            return
        _write(session, settings, account, strategy_id=strategy_id, pair=pair, candle_ts=candle_ts, outcome=OUT_NOT_TAKEN, reason=reason, detail=dict(detail or {}, sel=getattr(account, "pair_selection_mode", None)),
               is_paper=is_paper, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.debug("opportunity ledger not-taken failed (%s); ignored", type(exc).__name__)


# ------------------------------------------------------------------------------------------------ reading (the report and the daily summary)
def collect(session, *, since: datetime, until: datetime | None = None, account_id: str | None = None) -> list[dict]:
    """Ledger rows and the entry-stage rows of the same window, as plain dicts. Read-only."""
    q = select(DecisionLog.account_id, DecisionLog.strategy_id, DecisionLog.pair, DecisionLog.stage, DecisionLog.outcome, DecisionLog.reason_code, DecisionLog.candle_ts, DecisionLog.ts).where(
        DecisionLog.ts >= since, DecisionLog.stage.in_((STAGE, telemetry.STAGE_ENTRY)))
    if until is not None:
        q = q.where(DecisionLog.ts <= until)
    if account_id is not None:
        q = q.where(DecisionLog.account_id == account_id)
    return [dict(zip(("account_id", "strategy_id", "pair", "stage", "outcome", "reason_code", "candle_ts", "ts"), row)) for row in session.execute(q.order_by(DecisionLog.ts)).all()]


def aggregate(rows: list[dict]) -> dict:
    """{account_id: {strategy_id: {evaluated, signals, opened, no_signal: {code: n}, not_taken: {code: n}}}}. `not_taken` merges the ledger's own reasons with the entry-stage refusals; a stale signal is counted
    once per (pair, candle) even when both places wrote it."""
    out: dict = {}
    stale_seen: set = set()

    def slot(r):
        return out.setdefault(r["account_id"], {}).setdefault(r["strategy_id"], {"evaluated": 0, "signals": 0, "opened": 0, "no_signal": {}, "not_taken": {}})

    for r in rows:
        s = slot(r)
        if r["stage"] == STAGE:
            if r["outcome"] == OUT_NO_SIGNAL:
                s["evaluated"] += 1
                s["no_signal"][r["reason_code"]] = s["no_signal"].get(r["reason_code"], 0) + 1
            elif r["outcome"] == OUT_SIGNAL:
                s["evaluated"] += 1
                s["signals"] += 1
            elif r["outcome"] == OUT_NOT_TAKEN:
                if r["reason_code"] == R_STALE:
                    key = (r["account_id"], r["strategy_id"], r["pair"], r["candle_ts"])
                    if key in stale_seen:
                        continue
                    stale_seen.add(key)
                s["not_taken"][r["reason_code"]] = s["not_taken"].get(r["reason_code"], 0) + 1
        else:
            if r["outcome"] == telemetry.ACCEPTED and r["reason_code"] in (telemetry.R_ENTRY_SUBMITTED, telemetry.R_ENTRY_PAPER):
                s["opened"] += 1
            elif r["outcome"] in (telemetry.REJECTED, telemetry.SKIPPED):
                if r["reason_code"] == R_STALE:
                    key = (r["account_id"], r["strategy_id"], r["pair"], r["candle_ts"])
                    if key in stale_seen:
                        continue
                    stale_seen.add(key)
                s["not_taken"][r["reason_code"]] = s["not_taken"].get(r["reason_code"], 0) + 1
    return out


def plain(code: str) -> str:
    return PLAIN.get(code, "another reason")


def summary_lines(session, account_id: str, now: datetime, *, hours: int = 24, top: int = 3) -> list[str]:
    """Customer-safe text lines for the existing daily summary: per strategy, candles checked, signals and trades opened, and the main reasons a signal was not taken. Empty when there is nothing to say."""
    try:
        data = aggregate(collect(session, since=now - timedelta(hours=hours), account_id=account_id)).get(account_id, {})
        data = {k: v for k, v in data.items() if v["evaluated"] or v["opened"] or v["not_taken"]}
        if not data:
            return []
        lines = [f"Entry checks, last {hours} hours:"]
        for sid in (EMA_ID, DONCHIAN_ID, RSI_ID):
            s = data.get(sid)
            if not s:
                continue
            line = f"{LABEL[sid]}: {s['evaluated']} candle{'s' if s['evaluated'] != 1 else ''} checked, {s['signals']} signal{'s' if s['signals'] != 1 else ''}, {s['opened']} trade{'s' if s['opened'] != 1 else ''} opened"
            if s["not_taken"]:
                main = sorted(s["not_taken"].items(), key=lambda kv: (-kv[1], kv[0]))[:top]
                line += ". Not taken: " + ", ".join(f"{plain(c)} ({n})" for c, n in main)
            lines.append(line)
        return lines if len(lines) > 1 else []
    except Exception:  # noqa: BLE001
        return []
