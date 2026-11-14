"""The PURE rules of the strategy guards: decisions over plain numbers, no database, no configuration, no network, no clock.

The live path (`strategy_guards.py`) and the research backtester use THIS file, so the backtest exercises the same rules the bot runs.
The research branch carries a byte-identical copy; its pre-registration pins the SHA-256 of this file. Do not edit it without
re-registering. Nothing here can place, change or cancel an order. See docs/safety/strategy_guards.md."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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


