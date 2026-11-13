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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import protection_guard as pg
from .config import Settings
from .models import NON_TERMINAL_TRADE_STATUSES, TRADE_CLOSED, StrategyPause, Trade
from .notifications.telegram import TelegramNotifier
from .telemetry import STRATEGY_ID

logger = logging.getLogger(__name__)

R_EMA_GAP = "ema_gap_below_minimum"
R_STOP_COOLDOWN = "stop_loss_cooldown"
R_FEE_GUARD = "fee_guard_expected_move_too_small"
R_PORTFOLIO_CAP = "portfolio_cap_reached"
R_DAILY_LOSS = "strategy_daily_loss_limit"
R_LOSS_STREAK = "strategy_loss_streak_pause"
R_EXPECTANCY = "strategy_expectancy_pause"
R_PAUSED = "strategy_paused"
R_UNREADABLE = "strategy_guard_unreadable"
ALL_CODES = (R_EMA_GAP, R_STOP_COOLDOWN, R_FEE_GUARD, R_PORTFOLIO_CAP, R_DAILY_LOSS, R_LOSS_STREAK, R_EXPECTANCY, R_PAUSED, R_UNREADABLE)

# Trades closed by the infrastructure, not by the strategy: never counted.
INCIDENT_REASONS = frozenset({pg.EMERGENCY_REASON_STRUCTURAL, pg.EMERGENCY_REASON_TRANSIENT, pg.EMERGENCY_REASON_OTHER, "dust_unprotectable"})
STOP_REASON = "stop_loss"

# Customer-facing wording: fixed strings, no numbers from the customer's own results, no provider text.
MESSAGES = {
    R_STOP_COOLDOWN: "A trade on this pair was just stopped out, so the bot is waiting before it enters this pair again.",
    R_FEE_GUARD: "The expected move on this signal is too small compared with trading costs, so it was skipped.",
    R_PORTFOLIO_CAP: "The limit on open positions or total risk for this account is reached, so no new trade was opened.",
    R_DAILY_LOSS: "The daily loss limit was reached, so no new trades will be opened until the next UTC day.",
    R_LOSS_STREAK: "New trades are paused while the strategy is reviewed. Positions already open continue to be managed.",
    R_EXPECTANCY: "New trades are paused while the strategy is reviewed. Positions already open continue to be managed.",
    R_PAUSED: "New trades are paused while the strategy is reviewed. Positions already open continue to be managed.",
    R_UNREADABLE: "A safety check could not be completed, so no new trade was opened. It will be tried again on the next check.",
    R_EMA_GAP: None,  # a signal filter, like the flat-market filter: logged, never sent to the customer
}


@dataclass(frozen=True)
class GuardHit:
    code: str
    detail: dict = field(default_factory=dict)
    creates_pause: bool = False  # enforce mode: write a StrategyPause row
    pause_hours: float | None = None  # None = until the owner releases it
    owner_alert: bool = False  # tell the owner (once, when the pause is created)

    @property
    def message(self) -> str | None:
        return MESSAGES.get(self.code)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ======================================================================== pure decisions

def ema_gap_hit(gap_atr: float | None, min_gap_atr: float) -> GuardHit | None:
    """EMA crossover, false crosses in choppy markets: the fast and slow EMA must be at least `min_gap_atr` ATRs apart when the entry is
    taken. An unreadable gap refuses (fail closed). Costs: the entry is later and some good crosses are skipped; see the weakness table."""
    if min_gap_atr <= 0:
        return None
    if gap_atr is None or gap_atr != gap_atr:  # None or NaN
        return GuardHit(R_EMA_GAP, {"gap_atr": None, "min_gap_atr": min_gap_atr})
    if gap_atr < min_gap_atr:
        return GuardHit(R_EMA_GAP, {"gap_atr": round(gap_atr, 4), "min_gap_atr": min_gap_atr})
    return None


def fee_guard_hit(*, atr_pct: float | None, margin: float, fee_bps: float, slippage_bps: float, expected_move_atr: float) -> GuardHit | None:
    """Fee drag: the expected gross move (`expected_move_atr` ATRs, a proxy because the strategy has no profit target) must be at least
    `margin` times the round-trip cost (two taker fees plus the assumed slippage). Unreadable input refuses."""
    if margin <= 0:
        return None
    if atr_pct is None or atr_pct != atr_pct or atr_pct <= 0:
        return GuardHit(R_FEE_GUARD, {"atr_pct": None})
    cost = (2 * fee_bps + slippage_bps) / 10_000.0
    expected = expected_move_atr * atr_pct
    if expected < margin * cost:
        return GuardHit(R_FEE_GUARD, {"expected_pct": round(expected * 100, 4), "cost_pct": round(cost * 100, 4), "margin": margin})
    return None


def portfolio_hit(*, open_positions: int, open_risk_pct: float, new_risk_pct: float, max_positions: int, max_total_risk_pct: float) -> GuardHit | None:
    """One position per pair is structural (a unique index allows one non-terminal trade per account); this is the ceiling for when more
    than one strategy or position can exist: a cap on open positions and on the total risk across them."""
    if max_positions > 0 and open_positions + 1 > max_positions:
        return GuardHit(R_PORTFOLIO_CAP, {"open": open_positions, "max_positions": max_positions})
    if max_total_risk_pct > 0 and open_risk_pct + new_risk_pct > max_total_risk_pct + 1e-9:
        return GuardHit(R_PORTFOLIO_CAP, {"open_risk_pct": round(open_risk_pct, 3), "new_risk_pct": new_risk_pct, "max_total_risk_pct": max_total_risk_pct})
    return None


def cooldown_hit(last_stop_closed_at: datetime | None, now: datetime, hours: float) -> GuardHit | None:
    """After a stop loss on this pair, wait `hours` before a new entry on it (false crosses tend to cluster)."""
    if hours <= 0 or last_stop_closed_at is None:
        return None
    ends = _aware(last_stop_closed_at) + timedelta(hours=hours)
    if _aware(now) < ends:
        return GuardHit(R_STOP_COOLDOWN, {"cooldown_hours": hours, "minutes_left": int((ends - _aware(now)).total_seconds() // 60)})
    return None


def daily_loss_hit(*, net_pnl_today: float, capital: float | None, limit_pct: float) -> GuardHit | None:
    if limit_pct <= 0:
        return None
    if capital is None or capital != capital or capital <= 0:
        return GuardHit(R_DAILY_LOSS, {"capital": None})
    loss_pct = -net_pnl_today / capital * 100.0
    if loss_pct >= limit_pct - 1e-12:
        return GuardHit(R_DAILY_LOSS, {"loss_pct": round(loss_pct, 3), "limit_pct": limit_pct}, owner_alert=True)
    return None


def consecutive_losses(nets_newest_first: list[float]) -> int:
    """How many of the most recent trades in a row ended in a net loss (a break-even or a win ends the streak)."""
    n = 0
    for v in nets_newest_first:
        if v < 0:
            n += 1
        else:
            break
    return n


def loss_streak_hit(nets_newest_first: list[float], n_limit: int, pause_hours: float) -> GuardHit | None:
    if n_limit <= 0:
        return None
    streak = consecutive_losses(nets_newest_first)
    if streak >= n_limit:
        return GuardHit(R_LOSS_STREAK, {"consecutive_losses": streak, "limit": n_limit, "pause_hours": pause_hours},
                        creates_pause=True, pause_hours=pause_hours if pause_hours > 0 else None, owner_alert=True)
    return None


def expectancy_hit(net_rs_newest_first: list[float], window: int) -> GuardHit | None:
    """A strategy whose net expectancy over its last `window` trades is negative pauses itself. It does NOT resume by itself: only the
    owner's release ends it. With fewer than `window` usable trades there is nothing to judge yet."""
    if window <= 0 or len(net_rs_newest_first) < window:
        return None
    sample = net_rs_newest_first[:window]
    mean_r = sum(sample) / len(sample)
    if mean_r < 0:
        return GuardHit(R_EXPECTANCY, {"trades": window, "mean_net_r": round(mean_r, 4)}, creates_pause=True, pause_hours=None, owner_alert=True)
    return None


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
