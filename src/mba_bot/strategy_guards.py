"""Strategy guards: what limits the DOWNSIDE of a strategy (docs/safety/strategy_guards.md).

Pure decision functions plus the few database reads they need. They are called from `trader._maybe_enter` and, unchanged, from the
research backtester, so the backtest exercises the SAME rules the live path uses.

Rules this module keeps (CLAUDE.md):
  * A guard can only ever ADD a refusal of a NEW entry. It never lifts a refusal (the operator kill switch is checked before this
    and stays authoritative), never closes, moves or cancels anything, and never places an order.
  * STRATEGY_GUARDS_MODE off (the default) means none of this runs. observe: every guard computes and logs what it would have
    refused, and nothing is blocked or written. enforce: the first guard that fires blocks the entry.
  * Fail closed in enforce: a guard whose input cannot be read refuses the entry with a specific code instead of waving it through.
  * A threshold of 0 switches that single guard off.
  * Incident trades (emergency exits, dust) are infrastructure events, not strategy results: they never count towards a streak,
    an expectancy or a daily loss. Paper and live results are kept apart.
Fees in the expectancy are MODELLED (the trading path does not record exact fees): `fee_guard_fee_bps` per side on entry and exit notional.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import protection_guard as pg
from .config import Settings
from .models import NON_TERMINAL_TRADE_STATUSES, TRADE_CLOSED, StrategyPause, Trade
from .notifications.telegram import TelegramNotifier
from .telemetry import STRATEGY_ID

logger = logging.getLogger(__name__)

from .strategy_guard_rules import (  # noqa: F401  (re-exported: the pure rules live in strategy_guard_rules.py and are shared with the backtester)
    ALL_CODES, MESSAGES, R_DAILY_LOSS, R_EMA_GAP, R_EXPECTANCY, R_FEE_GUARD, R_LOSS_STREAK, R_PAUSED, R_PORTFOLIO_CAP, R_STOP_COOLDOWN,
    R_UNREADABLE, STOP_REASON, GuardHit, _aware, consecutive_losses, cooldown_hit, daily_loss_hit, ema_gap_hit, expectancy_hit,
    fee_guard_hit, loss_streak_hit, portfolio_hit,
)

# Trades closed by the infrastructure, not by the strategy: never counted.
INCIDENT_REASONS = frozenset({pg.EMERGENCY_REASON_STRUCTURAL, pg.EMERGENCY_REASON_TRANSIENT, pg.EMERGENCY_REASON_OTHER, "dust_unprotectable"})


# ======================================================================== trade results

@dataclass(frozen=True)
class Result:
    closed_at: datetime
    pair: str | None
    close_reason: str | None
    net: float  # gross pnl minus modelled fees, in the quote currency
    net_r: float  # net divided by the money at risk (|entry - stop| x quantity)


def trade_result(t: Trade, fee_bps: float) -> Result | None:
    """The modelled net result of one closed trade, or None if it cannot be judged (missing or odd numbers)."""
    try:
        if t.pnl is None or t.closed_at is None or t.exit_price is None:
            return None
        risk = abs(t.entry_price - t.stop_loss_price) * t.quantity
        if not (risk > 0) or risk != risk:
            return None
        fees = fee_bps / 10_000.0 * (t.entry_price * t.quantity + t.exit_price * t.quantity)
        net = float(t.pnl) - fees
        return Result(closed_at=_aware(t.closed_at), pair=t.pair, close_reason=t.close_reason, net=net, net_r=net / risk)
    except (TypeError, ValueError):
        return None


def closed_results(session: Session, account_id: str, *, is_paper: bool, fee_bps: float, since: datetime | None = None, limit: int = 200) -> list[Result]:
    """This account's closed STRATEGY trades in this mode, newest first, incident trades excluded."""
    q = select(Trade).where(Trade.account_id == account_id, Trade.status == TRADE_CLOSED, Trade.is_paper.is_(is_paper), Trade.closed_at.isnot(None))
    if since is not None:
        q = q.where(Trade.closed_at > since)
    rows = session.scalars(q.order_by(Trade.closed_at.desc()).limit(limit * 4 + 20)).all()  # headroom: incident trades are skipped below
    out = []
    for t in rows:
        if t.close_reason in INCIDENT_REASONS:
            continue
        r = trade_result(t, fee_bps)
        if r is not None:
            out.append(r)
        if len(out) >= limit:
            break
    return out


# ======================================================================== pauses

def pauses(session: Session, account_id: str, strategy_id: str, is_paper: bool) -> list[StrategyPause]:
    return list(session.scalars(select(StrategyPause).where(StrategyPause.account_id == account_id, StrategyPause.strategy_id == strategy_id,
                                                            StrategyPause.is_paper.is_(is_paper)).order_by(StrategyPause.paused_at)))


def is_active(p: StrategyPause, now: datetime) -> bool:
    return p.released_at is None and (p.resume_at is None or _aware(p.resume_at) > _aware(now))


def window_start(rows: list[StrategyPause], code: str, now: datetime) -> datetime | None:
    """When the last pause of this kind ENDED (released, or its time ran out): only trades closed after that count towards the same guard,
    so an owner release or an expired pause is not immediately undone by the same old trades."""
    ends = []
    for p in rows:
        if p.code != code:
            continue
        if p.released_at is not None:
            ends.append(_aware(p.released_at))
        elif p.resume_at is not None and _aware(p.resume_at) <= _aware(now):
            ends.append(_aware(p.resume_at))
    return max(ends) if ends else None


def create_pause(session: Session, account_id: str, strategy_id: str, hit: GuardHit, *, is_paper: bool, now: datetime) -> StrategyPause:
    resume = _aware(now) + timedelta(hours=hit.pause_hours) if hit.pause_hours else None
    detail = ", ".join(f"{k}={v}" for k, v in hit.detail.items())[:200]
    row = StrategyPause(account_id=account_id, strategy_id=strategy_id, code=hit.code, is_paper=is_paper, paused_at=_aware(now), resume_at=resume, detail=detail)
    session.add(row)
    session.commit()  # its own short synchronous transaction (scheduler sessions never flush implicitly)
    return row


def release_pauses(session: Session, account_id: str, strategy_id: str, *, is_paper: bool | None, now: datetime) -> int:
    n = 0
    for p in session.scalars(select(StrategyPause).where(StrategyPause.account_id == account_id, StrategyPause.strategy_id == strategy_id,
                                                         StrategyPause.released_at.is_(None))):
        if is_paper is not None and p.is_paper != is_paper:
            continue
        p.released_at = _aware(now)
        n += 1
    return n


# ======================================================================== the entry check

def open_position_numbers(session: Session, account_id: str, default_risk_pct: float) -> tuple[int, float]:
    """(open positions, their total risk %). Risk per open position is the account's configured risk %, a proxy (the money at risk
    is not stored per trade)."""
    n = len(session.scalars(select(Trade.id).where(Trade.account_id == account_id, Trade.status.in_(NON_TERMINAL_TRADE_STATUSES))).all())
    return n, n * default_risk_pct


def evaluate(session: Session, account, settings: Settings, *, signal, is_paper: bool, now: datetime, strategy_id: str = STRATEGY_ID) -> list[GuardHit]:
    """Every guard's verdict for a NEW entry, most decisive first. Pure with respect to trading: it reads and decides. In observe mode the
    caller logs all of them; in enforce mode it acts on the first. Raises only if the database cannot be read; the caller treats that as
    a refusal in enforce mode."""
    hits: list[GuardHit] = []
    now = _aware(now)

    rows = pauses(session, account.id, strategy_id, is_paper)
    active = [p for p in rows if is_active(p, now)]
    if active:
        hits.append(GuardHit(R_PAUSED, {"since": _aware(active[0].paused_at).isoformat(timespec="minutes"), "pause_code": active[0].code}))

    fee_bps = settings.fee_guard_fee_bps
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if settings.strategy_daily_loss_pct > 0:
        today = [r for r in closed_results(session, account.id, is_paper=is_paper, fee_bps=fee_bps, since=day_start - timedelta(microseconds=1))]
        capital = account.paper_balance if is_paper else account.allocated_capital
        h = daily_loss_hit(net_pnl_today=sum(r.net for r in today), capital=capital, limit_pct=settings.strategy_daily_loss_pct)
        if h:
            hits.append(h)

    if settings.ema_stop_cooldown_hours > 0:
        recent = closed_results(session, account.id, is_paper=is_paper, fee_bps=fee_bps, since=now - timedelta(hours=settings.ema_stop_cooldown_hours), limit=50)
        stops = [r.closed_at for r in recent if r.close_reason == STOP_REASON and (r.pair in (None, account.pair))]
        h = cooldown_hit(max(stops) if stops else None, now, settings.ema_stop_cooldown_hours)
        if h:
            hits.append(h)

    if settings.strategy_pause_after_losses > 0:
        start = window_start(rows, R_LOSS_STREAK, now)
        res = closed_results(session, account.id, is_paper=is_paper, fee_bps=fee_bps, since=start, limit=max(10, settings.strategy_pause_after_losses + 5))
        h = loss_streak_hit([r.net for r in res], settings.strategy_pause_after_losses, settings.strategy_streak_pause_hours)
        if h:
            hits.append(h)

    if settings.strategy_expectancy_window > 0:
        start = window_start(rows, R_EXPECTANCY, now)
        res = closed_results(session, account.id, is_paper=is_paper, fee_bps=fee_bps, since=start, limit=settings.strategy_expectancy_window)
        h = expectancy_hit([r.net_r for r in res], settings.strategy_expectancy_window)
        if h:
            hits.append(h)

    h = fee_guard_hit(atr_pct=getattr(signal, "atr_pct", None), margin=settings.fee_guard_margin, fee_bps=fee_bps,
                      slippage_bps=settings.fee_guard_slippage_bps, expected_move_atr=settings.fee_guard_expected_move_atr)
    if h:
        hits.append(h)

    if settings.max_open_positions > 0 or settings.max_total_risk_pct > 0:
        n_open, open_risk = open_position_numbers(session, account.id, account.risk_pct)
        h = portfolio_hit(open_positions=n_open, open_risk_pct=open_risk, new_risk_pct=account.risk_pct,
                          max_positions=settings.max_open_positions, max_total_risk_pct=settings.max_total_risk_pct)
        if h:
            hits.append(h)

    h = ema_gap_hit(getattr(signal, "gap_atr", None), settings.ema_min_gap_atr)
    if h:
        hits.append(h)
    return hits


# ======================================================================== owner alert

def owner_text(account_id: str, strategy_id: str, hit: GuardHit) -> str:
    """Fixed wording with the masked account id and the numbers of the rule that fired: no e-mail, no secret."""
    nums = ", ".join(f"{k}={v}" for k, v in hit.detail.items())
    ends = "until you release it" if hit.pause_hours is None else f"for {hit.pause_hours:g} hours"
    return f"Strategy guard: {hit.code} for {strategy_id}, account {str(account_id)[:8]}... ({nums}). " + (
        f"New entries are paused {ends}." if hit.creates_pause else "New entries are refused for now.")


async def alert_owner(settings: Settings, text: str) -> bool:
    """Telegram to the owner. Never raises. Without a bot token and an owner chat it only logs."""
    logger.warning("%s", text)
    if not (settings.telegram_bot_token and settings.owner_alert_chat_id):
        return False
    try:
        await TelegramNotifier(settings.telegram_bot_token).send(settings.owner_alert_chat_id, text)
        return True
    except Exception:  # noqa: BLE001 - an owner alert must never break a trading cycle
        logger.warning("an owner alert from the strategy guards could not be delivered")
        return False
