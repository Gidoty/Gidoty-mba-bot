"""How many trades an account may hold at once, and which pairs it trades. Pure functions: no I/O, no clock, no database.

Rules (docs/safety/multi_trade.md):
* A LIVE account is capped at 1 trade here in code, whatever any setting says. Only a PAPER account can use grade 2 or 3.
* The configured cap (MAX_OPEN_POSITIONS, default 1) is clamped to 1..HARD_MAX_TRADES (3).
* The market grade (1, 2 or 3) lowers the cap further. Any missing, malformed or unclear input means grade 1.
* A lower grade never closes a trade. It only blocks NEW entries.

The grade thresholds below (and their Settings defaults) are STARTING VALUES, not tuned on any data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

EMA_STRATEGY_ID = "ema_cross_atr_v1"
HARD_MAX_TRADES = 3

# Reason codes (decision log / journal). Max 48 characters.
R_SLOT_FULL = "trade_slots_full"
R_TOTAL_RISK_CAP = "total_open_risk_cap"
R_CAPITAL_SHORT = "free_capital_short"


def strategy_of(trade) -> str:
    """The strategy that opened a trade. Empty or missing means ema_cross_atr_v1 (no backfill of old rows)."""
    value = getattr(trade, "strategy_id", None)
    return value if value else EMA_STRATEGY_ID


NEW_STRATEGY_IDS = ("rsi_mr_v1", "donchian_breakout_v1")


def account_strategies(account) -> frozenset:
    """The new strategies this account is opted in to: the owner-set `extra_strategies` column, comma separated. NULL, empty, anything that is not a
    known strategy id (and the EMA id, which needs no opt-in) is ignored. A paper account that is not opted in keeps the EMA path and Auto Select."""
    raw = getattr(account, "extra_strategies", None) or ""
    return frozenset(p for p in (part.strip().lower() for part in raw.split(",")) if p in NEW_STRATEGY_IDS)


def trading_pairs(account) -> list[str]:
    """The pairs a multi-trade paper account may trade: its own `pair` first, then the owner-set `pair_list`, no duplicates, at most
    HARD_MAX_TRADES. An account with no pair_list trades exactly its one pair."""
    out: list[str] = []
    primary = getattr(account, "pair", None)
    if primary:
        out.append(primary)
    raw = getattr(account, "pair_list", None) or ""
    for part in raw.split(","):
        p = part.strip().upper()
        if p and p not in out:
            out.append(p)
    return out[:HARD_MAX_TRADES]


def configured_cap(configured) -> int:
    """MAX_OPEN_POSITIONS clamped to 1..HARD_MAX_TRADES. Zero, negative, missing or not a number is 1."""
    try:
        n = int(configured)
    except (TypeError, ValueError, OverflowError):  # int(float("inf")) raises OverflowError
        return 1
    return max(1, min(HARD_MAX_TRADES, n))


def slot_cap(*, is_paper: bool, configured, grade: int) -> int:
    """How many non-terminal trades the account may have, including the one being considered. LIVE is always 1."""
    if not is_paper:
        return 1
    g = grade if isinstance(grade, int) and not isinstance(grade, bool) else 1
    return max(1, min(configured_cap(configured), HARD_MAX_TRADES, g))


@dataclass(frozen=True)
class GradeThresholds:
    grade2_min_trades: int = 10  # closed trades needed for grade 2, and the window they are judged over
    grade3_min_trades: int = 20
    extreme_atr_multiple: float = 3.0  # ATR% above this many times its rolling average is "extreme"
    daily_loss_fraction: float = 0.5  # grade 3 needs today's loss under this fraction of STRATEGY_DAILY_LOSS_PCT


@dataclass(frozen=True)
class GradeInputs:
    """Everything None means 'not known'. Unknown is never assumed fine."""
    atr_pct: float | None = None
    atr_avg_pct: float | None = None  # rolling average of ATR%
    flat_threshold: float | None = None  # the existing flat-market filter factor (ATR% below avg * this is flat)
    nets_newest_first: tuple[float, ...] | None = None  # net results (after modelled fees) of the most recent closed trades
    pause_active: bool | None = None
    daily_pnl: float | None = None
    capital: float | None = None
    daily_loss_limit_pct: float | None = None


@dataclass(frozen=True)
class Grade:
    level: int
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def text(self) -> str:
        return f"grade {self.level}" + (f" ({'; '.join(self.reasons)})" if self.reasons else "")


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def grade_from(i: GradeInputs, t: GradeThresholds = GradeThresholds()) -> Grade:
    """1 = poor or unknown, 2 = ok, 3 = very good. Fail closed: anything missing or unclear is grade 1."""
    for name in ("atr_pct", "atr_avg_pct", "flat_threshold"):
        if not _ok(getattr(i, name)):
            return Grade(1, (f"input_missing:{name}",))
    if i.atr_avg_pct <= 0 or i.atr_pct < 0 or i.flat_threshold < 0:
        return Grade(1, ("input_unclear:atr",))
    if i.pause_active is None:
        return Grade(1, ("input_missing:pause_state",))
    if i.nets_newest_first is None or not all(_ok(v) for v in i.nets_newest_first):
        return Grade(1, ("input_missing:closed_trades",))
    if i.atr_pct < i.atr_avg_pct * i.flat_threshold:
        return Grade(1, ("flat_market",))
    if i.atr_pct > i.atr_avg_pct * t.extreme_atr_multiple:
        return Grade(1, ("volatility_extreme",))
    if i.pause_active:
        return Grade(1, ("strategy_paused",))
    last = i.nets_newest_first[: t.grade2_min_trades]
    if len(last) < t.grade2_min_trades:
        return Grade(1, (f"fewer_than_{t.grade2_min_trades}_closed_trades",))
    if sum(last) < 0:
        return Grade(1, (f"last_{t.grade2_min_trades}_net_negative",))

    why: list[str] = []
    window = i.nets_newest_first[: t.grade3_min_trades]
    if len(window) < t.grade3_min_trades:
        why.append(f"fewer_than_{t.grade3_min_trades}_closed_trades")
    elif not sum(window) > 0:
        why.append(f"last_{t.grade3_min_trades}_not_net_positive")
    if not (_ok(i.capital) and i.capital > 0 and _ok(i.daily_pnl) and _ok(i.daily_loss_limit_pct) and i.daily_loss_limit_pct > 0):
        why.append("daily_loss_limit_unknown")
    elif -i.daily_pnl / i.capital * 100.0 >= t.daily_loss_fraction * i.daily_loss_limit_pct:
        why.append("daily_loss_over_half_limit")
    return Grade(2, tuple(why)) if why else Grade(3, ())


def free_capital(balance: float, open_notionals) -> float:
    """Capital not tied up in open trades (paper). Notional is counted at 1x, so this is conservative for Futures."""
    return float(balance) - float(sum(open_notionals))


def open_risk_pct(open_risk_amounts, balance: float) -> float | None:
    """Sum of the money at risk in open trades (entry to stop times quantity) as a percent of balance. None if it cannot be computed."""
    if not _ok(balance) or balance <= 0:
        return None
    amounts = list(open_risk_amounts)
    if not all(_ok(a) and a >= 0 for a in amounts):
        return None
    return sum(amounts) / balance * 100.0
