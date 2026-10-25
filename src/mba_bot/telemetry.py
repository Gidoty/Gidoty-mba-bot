"""Research telemetry: why each trade was taken, and which opportunities were not.

Everything here is OBSERVATION ONLY.

* It is off unless `Settings.trade_telemetry` (TRADE_TELEMETRY) is on; with it
  off every function below returns immediately and no row is ever created.
* Nothing in the trading path reads what it writes.
* It never raises into the caller. A telemetry bug must not be able to cost a
  customer a trade, so every public function swallows its own failures
  (logging only the exception TYPE, never its text).
* Queuing a row (log_decision, record_entry) only adds it to the session in
  memory: no flush, no commit. Scheduler sessions do not autoflush (see
  db.session_scope), so such rows are written by the next commit the trading
  code makes. Two helpers do write, and only when the flag is on:
    - write_with_trade (paper entry): inside a SAVEPOINT it flushes the
      telemetry rows just before the trade's own commit. The caller has
      already flushed the trade and commits right after, with no await in
      between.
    - write_and_commit (live entry): after the live trade has been opened and
      committed by the lifecycle code, it queues the rows in a SAVEPOINT,
      flushes, and COMMITS once, synchronously. That commit also writes
      whatever else is pending in the same session at that moment, slightly
      earlier than the trading code alone would have.
  In both, a failing telemetry row rolls back only its own SAVEPOINT and the
  trade is unaffected. The scheduler's rule still holds: the write lock is
  taken only for a short synchronous stretch and is never held across an
  await. With the flag off none of this runs.
* It stores no secrets: no keys, no tokens, no customer text, no exchange
  error text. Only numbers and short machine codes.
* The decision log records actionable moments only (a crossover was seen, an
  entry was attempted, an Auto scan picked or skipped a pair), never idle
  polls, writes the same decision on the same candle once, and is capped per
  account per day, so it cannot grow without bound.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import inspect

from . import db as dbmod
from .config import Settings
from .features import compute_features
from .models import DecisionLog, TradeTelemetry
from .strategy import SignalType, StrategySignal

logger = logging.getLogger(__name__)

STRATEGY_ID = "ema_cross_atr_v1"
SCHEMA_VERSION = 1

# --- stages / outcomes / reason codes (short, stable, machine-readable) ---------------
STAGE_SIGNAL = "signal"
STAGE_ENTRY = "entry"
STAGE_AUTO_SCAN = "auto_scan"

ACCEPTED = "accepted"
REJECTED = "rejected"
SKIPPED = "skipped"

R_SIGNAL_ACTIONABLE = "signal_actionable"
R_FLAT_MARKET = "flat_market_filter"
R_SPOT_LONG_ONLY = "spot_long_only"
R_PAIR_UNAVAILABLE = "pair_not_available"
R_WITHDRAWAL_BLOCKED = "withdrawal_blocked"
R_WITHDRAWAL_DETECTED = "withdrawal_permission_detected"
R_NO_CAPITAL_SET = "no_allocated_capital"
R_NO_FREE_BALANCE = "no_free_balance"
R_SIZE_ZERO = "size_zero"
R_SIZE_ZERO_CEILING = "size_zero_after_ceilings"
R_PREFLIGHT_REFUSED = "preflight_refused"
R_UNSUPPORTED = "unsupported_account_or_symbol"
R_NO_PROTECTION = "protective_stop_unavailable"
R_LEVERAGE_UNREADABLE = "leverage_unreadable"
R_EXCHANGE_FILTERS = "below_exchange_minimum_or_filters"
R_CONCURRENT_TRADE = "concurrent_trade_exists"
R_ENTRY_SUBMITTED = "entry_submitted"
R_ENTRY_PAPER = "entry_paper"
R_AUTO_PICK = "auto_pick"
R_AUTO_SKIP_CAPITAL = "auto_skip_insufficient_capital"
R_AUTO_FLAT = "auto_candidate_flat_filter"

_DETAIL_MAX = 1500
_DEDUPE_MAX = 4000
_DAILY_CAP_PER_ACCOUNT = 300

_seen: dict[tuple, float] = {}
_daily: dict[tuple[str, str], int] = {}


@dataclass(frozen=True)
class ScanInfo:
    """What an Auto Select scan saw this cycle (None for a manual pair)."""

    picked: str
    rank: int
    score: float | None
    ranked: list[tuple[str, float]] = field(default_factory=list)
    skipped_capital: list[str] = field(default_factory=list)
    flat_filtered: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EntryContext:
    """Decision-time facts, built once per cycle that has an actionable signal."""

    signal: StrategySignal
    price: float
    candle_ts: int | None
    timeframe: str
    features: dict
    scan: ScanInfo | None = None


_schema_state: dict = {"engine": None, "ok": False}


def _schema_ready() -> bool:
    """True when both telemetry tables exist. Checked once per database engine. If the
    flag is on but the migration was not applied, telemetry switches itself off (and says
    so once) instead of letting every insert fail inside the trading cycle."""
    engine = dbmod._engine
    if engine is None:
        return False
    if _schema_state["engine"] is engine:
        return _schema_state["ok"]
    try:
        ok = {"trade_telemetry", "decision_log"} <= set(inspect(engine).get_table_names())
    except Exception:  # noqa: BLE001
        ok = False
    _schema_state.update(engine=engine, ok=ok)
    if not ok:
        logger.warning("TRADE_TELEMETRY is on but the telemetry tables are missing; run `alembic upgrade head`. Telemetry is disabled for this process.")
    return ok


def enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "trade_telemetry", False)) and _schema_ready()


def write_with_trade(session, fn) -> None:
    """For a trade the caller is about to commit itself. Runs `fn` (which queues telemetry rows)
    inside a SAVEPOINT and flushes it, so a failing telemetry row is rolled back on its own and can
    never take the trade down with it. The caller must already have flushed its trade, and must commit
    right after with no await in between (the flush takes the database write lock)."""
    try:
        with session.begin_nested():
            fn()
            session.flush()
    except Exception as exc:  # noqa: BLE001
        _quiet("write_with_trade", exc)


def write_and_commit(session, fn) -> None:
    """For a trade that is already committed (the live path). Queues the rows inside a SAVEPOINT, then
    commits straight away: a short synchronous stretch, so no write lock is held across a later await.
    A failing telemetry row is rolled back on its own and the commit still goes ahead, so the rest of the
    cycle's state is written exactly as it would have been without telemetry."""
    try:
        with session.begin_nested():
            fn()
            session.flush()
    except Exception as exc:  # noqa: BLE001
        _quiet("write_and_commit", exc)
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        _quiet("write_and_commit.commit", exc)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass


def strategy_params(settings: Settings) -> dict:
    return {
        "timeframe": settings.timeframe,
        "ohlcv_limit": settings.ohlcv_limit,
        "ema_fast_period": settings.ema_fast_period,
        "ema_slow_period": settings.ema_slow_period,
        "atr_period": settings.atr_period,
        "atr_stop_multiplier": settings.atr_stop_multiplier,
        "atr_flat_threshold": settings.atr_flat_threshold,
        "atr_rolling_window": settings.atr_rolling_window,
    }


def strategy_version(settings: Settings) -> str:
    blob = json.dumps(strategy_params(settings), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _json(value: dict | None) -> str | None:
    if not value:
        return None
    try:
        text = json.dumps(value, separators=(",", ":"), default=str, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if len(text) > _DETAIL_MAX:
        return json.dumps({"truncated": True})
    return text


def _quiet(where: str, exc: BaseException) -> None:
    # Type only: exception text can carry values we do not want in logs.
    logger.debug("telemetry %s failed (%s); ignored", where, type(exc).__name__)


def build_entry_context(
    candles: list, signal: StrategySignal, settings: Settings, *, now: datetime, scan: ScanInfo | None = None
) -> EntryContext | None:
    """Never raises; None when telemetry is off or the context cannot be built."""
    if not enabled(settings):
        return None
    try:
        feats = compute_features(
            candles,
            ema_fast_period=settings.ema_fast_period,
            ema_slow_period=settings.ema_slow_period,
            atr_period=settings.atr_period,
            atr_rolling_window=settings.atr_rolling_window,
            bar_seconds=_bar_seconds(settings.timeframe),
            now_ms=int(now.timestamp() * 1000),
        )
        return EntryContext(
            signal=signal,
            price=candles[-1].close,
            candle_ts=int(candles[-1].timestamp),
            timeframe=settings.timeframe,
            features=feats,
            scan=scan,
        )
    except Exception as exc:  # noqa: BLE001
        _quiet("build_entry_context", exc)
        return None


def _bar_seconds(timeframe: str) -> int:
    try:
        unit = timeframe[-1]
        n = int(timeframe[:-1])
        return n * {"m": 60, "h": 3600, "d": 86400}[unit]
    except (KeyError, ValueError, IndexError):
        return 3600


def _allow(account_id: str, key: tuple, now_ts: float, day: str) -> bool:
    if key in _seen:
        return False
    if _daily.get((account_id, day), 0) >= _DAILY_CAP_PER_ACCOUNT:
        return False
    if len(_seen) >= _DEDUPE_MAX:
        # Drop the oldest half. Order of insertion is the order of the dict.
        for old in list(_seen)[: _DEDUPE_MAX // 2]:
            _seen.pop(old, None)
    if len(_daily) > 2000:
        _daily.clear()
    _seen[key] = now_ts
    _daily[(account_id, day)] = _daily.get((account_id, day), 0) + 1
    return True


def log_decision(
    session,
    settings: Settings,
    account,
    *,
    stage: str,
    outcome: str,
    reason: str,
    is_paper: bool,
    ctx: EntryContext | None = None,
    pair: str | None = None,
    signal: str | None = None,
    price: float | None = None,
    candle_ts: int | None = None,
    detail: dict | None = None,
    trade_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Queue one decision row. Never raises, never flushes."""
    if not enabled(settings):
        return
    try:
        pair_value = pair or account.pair
        if ctx is not None:
            signal = signal or ctx.signal.signal.value
            price = price if price is not None else ctx.price
            candle_ts = candle_ts if candle_ts is not None else ctx.candle_ts
        stamp = now or datetime.now().astimezone()
        day = stamp.strftime("%Y-%m-%d")
        key = (account.id, pair_value, stage, outcome, reason, candle_ts)
        if not _allow(account.id, key, time.monotonic(), day):
            return
        session.add(
            DecisionLog(
                ts=stamp,
                account_id=account.id,
                mode="paper" if is_paper else "live",
                market_type=account.market_type,
                pair=pair_value,
                strategy_id=STRATEGY_ID,
                stage=stage,
                outcome=outcome,
                reason_code=reason[:48],
                signal=signal,
                price=price,
                candle_ts=candle_ts,
                trade_id=trade_id,
                detail_json=_json(detail),
            )
        )
    except Exception as exc:  # noqa: BLE001
        _quiet("log_decision", exc)


def record_entry(
    session,
    settings: Settings,
    account,
    trade,
    *,
    ctx: EntryContext | None,
    is_paper: bool,
    effective_capital: float | None,
    risk_amount: float | None,
    quantity: float | None,
    stop_price: float | None,
    leverage: float | None = None,
) -> None:
    """Queue the per-trade telemetry row for a trade just created. Never raises."""
    if not enabled(settings) or ctx is None or trade is None:
        return
    try:
        if any(isinstance(o, TradeTelemetry) and o.trade_id == trade.id for o in session.new):
            return
        sig = ctx.signal
        price = ctx.price
        notional = (quantity or 0.0) * price if quantity else None
        stop_pct = abs(price - stop_price) / price if (stop_price and price) else None
        scan = ctx.scan
        session.add(
            TradeTelemetry(
                trade_id=trade.id,
                schema_version=SCHEMA_VERSION,
                strategy_id=STRATEGY_ID,
                strategy_version=strategy_version(settings),
                strategy_params_json=_json(strategy_params(settings)),
                mode="paper" if is_paper else "live",
                market_type=account.market_type,
                pair=trade.pair or account.pair,
                pair_selection_mode=getattr(account, "pair_selection_mode", None),
                timeframe=ctx.timeframe,
                signal_type=sig.signal.value,
                signal_reason=(sig.reason or "")[:120],
                signal_price=price,
                candle_ts=ctx.candle_ts,
                atr=sig.atr,
                atr_pct=sig.atr_pct,
                fast_ema=sig.fast_ema,
                slow_ema=sig.slow_ema,
                effective_capital=effective_capital,
                risk_pct=getattr(account, "risk_pct", None),
                planned_risk_amount=risk_amount,
                planned_quantity=quantity,
                planned_notional=notional,
                stop_price=stop_price,
                stop_distance_pct=stop_pct,
                account_leverage=leverage,
                auto_rank=scan.rank if scan else None,
                auto_score=scan.score if scan else None,
                auto_candidates=len(scan.ranked) if scan else None,
                features_json=_json(ctx.features),
            )
        )
    except Exception as exc:  # noqa: BLE001
        _quiet("record_entry", exc)


def signal_stage(
    session, settings: Settings, account, ctx: EntryContext | None, signal: StrategySignal, *, is_paper: bool, now: datetime
) -> None:
    """Log what the strategy concluded when the account is flat: an actionable
    crossover, a crossover rejected by the flat-market filter, or a bearish
    crossover that a Spot account cannot act on."""
    if not enabled(settings):
        return
    try:
        if signal.signal == SignalType.NONE:
            if "flat" in (signal.reason or ""):
                log_decision(
                    session, settings, account, stage=STAGE_SIGNAL, outcome=REJECTED, reason=R_FLAT_MARKET, is_paper=is_paper,
                    ctx=ctx, now=now,
                    detail={"atr_pct": signal.atr_pct, "fast": signal.fast_ema, "slow": signal.slow_ema},
                )
            return
        if signal.signal == SignalType.BEARISH_CROSSOVER and account.market_type != "futures":
            log_decision(
                session, settings, account, stage=STAGE_SIGNAL, outcome=SKIPPED, reason=R_SPOT_LONG_ONLY, is_paper=is_paper,
                ctx=ctx, now=now,
            )
            return
        log_decision(
            session, settings, account, stage=STAGE_SIGNAL, outcome=ACCEPTED, reason=R_SIGNAL_ACTIONABLE, is_paper=is_paper,
            ctx=ctx, now=now,
            detail={"atr_pct": signal.atr_pct, "gap_atr": (ctx.features.get("ema_gap_atr") if ctx else None)},
        )
    except Exception as exc:  # noqa: BLE001
        _quiet("signal_stage", exc)


def _bucket_ms(now: datetime, seconds: int = 3600) -> int:
    return int(now.timestamp() // seconds * seconds * 1000)


def log_auto_scan(session, settings: Settings, account, scan: ScanInfo | None, events: list[dict], *, is_paper: bool, now: datetime) -> None:
    """Summarise an Auto scan: pairs skipped for capital, candidates dropped by the
    flat-market filter, and the pick. Never raises."""
    if not enabled(settings):
        return
    try:
        for ev in events:
            log_decision(
                session, settings, account, stage=STAGE_AUTO_SCAN, outcome=SKIPPED, reason=ev["reason"], is_paper=is_paper,
                pair=ev.get("pair"), candle_ts=ev.get("candle_ts") or _bucket_ms(now), now=now,
            )
        if scan is not None:
            log_decision(
                session, settings, account, stage=STAGE_AUTO_SCAN, outcome=ACCEPTED, reason=R_AUTO_PICK, is_paper=is_paper,
                pair=scan.picked, candle_ts=_bucket_ms(now), now=now,
                detail={"rank": scan.rank, "score": scan.score, "ranked": [[p, round(s, 4)] for p, s in scan.ranked[:6]]},
            )
    except Exception as exc:  # noqa: BLE001
        _quiet("log_auto_scan", exc)


def reset_for_tests() -> None:
    _seen.clear()
    _daily.clear()
    _schema_state.update(engine=None, ok=False)
