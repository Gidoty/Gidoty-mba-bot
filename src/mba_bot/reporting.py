"""Per-account results reporting (docs/safety/reporting.md). Ships behind REPORTING_V2 (default off). Pure functions: no I/O, no clock, no database.

One account's CLOSED trades are grouped by mode (live, demo, practice), strategy and pair. Paper, demo and live are never added together. For each group:
  * gross P&L is the sum of `trades.pnl` (before exchange fees and before Futures funding);
  * fees are the sum of the real commissions captured from the fills (`entry_fee_quote` + `exit_fee_quote`, PR fee capture). A group is `fees_complete` only if EVERY closed trade in it has both numbers;
  * Futures funding is the sum of `funding_quote` (signed from the account's side: negative = paid). A Spot trade has none (known, zero). A Futures trade whose funding could not be read has NULL: unknown, never guessed;
  * net P&L (gross - fees + funding) is given ONLY for a live or demo group where EVERY trade has both fee numbers AND its funding known. Otherwise it is None and the wording says "before fees" or "before funding";
  * practice (paper) results are SIMULATED: no fees, no slippage, no funding, never net.
Nothing here reads a key or a secret: every field is a number, a count or a label."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MODE_LIVE, MODE_DEMO, MODE_PAPER = "live", "demo", "paper"
EMA_ID = "ema_cross_atr_v1"

MODE_LABEL = {
    MODE_LIVE: "Live (real funds)",
    MODE_DEMO: "Demo (Binance demo system, no real money)",
    MODE_PAPER: "Practice (simulated: no fees, no slippage)",
}
GIVEBACK_REPORT_R = 1.0  # a trade "closed far below its peak" when it closed more than this many R below its best price
ASSUMED_FEE_BPS, ASSUMED_SLIPPAGE_BPS = 5.0, 11.0  # the give-back test judges "below entry net of costs" with these assumed costs (docs/safety/give_back_alarm.md)
FUNDING_NOTE = "Net is shown only when the real fees and the Futures funding of every trade in the figure are known; funding is shown on its own line."


def mode_of(trade) -> str:
    if getattr(trade, "is_demo", False):
        return MODE_DEMO
    return MODE_PAPER if getattr(trade, "is_paper", False) else MODE_LIVE


def funding_applies(trade) -> bool:
    """Funding exists only on Futures. A trade's market is not stored on it, so it is read from the protective order it was given: Futures stops are "stop_market_close" or "stop_market_reduce", Spot stops
    "stop_loss" or "stop_loss_limit". A trade with no protective kind (a very old or practice row) is treated as Spot: no funding claimed, and a practice trade never gets a net figure anyway."""
    return str(getattr(trade, "protective_kind", None) or "").startswith("stop_market")


def giveback_flags(trade) -> tuple[bool, bool, bool, bool]:
    """(peak known, reached +1 R, gave it back to a loss, closed far below its peak) for a CLOSED trade; all False when the peak, the initial stop or the exit price is missing. R is the distance from the entry to
    the INITIAL stop. "Gave it back to a loss": the best price was at least +1 R in profit and the exit was below the entry by less than the round-trip cost (or above, for a short): both fees and the slippage at the
    ASSUMED rates, so it is an estimate, and it needs no fee data. "Far below its peak": the exit was more than GIVEBACK_REPORT_R x R worse than the best price."""
    peak, entry, stop, exit_ = (_num(getattr(trade, k, None)) for k in ("peak_price", "entry_price", "initial_stop_price", "exit_price"))
    if peak is None or entry is None or stop is None or exit_ is None or abs(entry - stop) <= 0:
        return False, False, False, False
    r = abs(entry - stop)
    long_side = getattr(trade, "side", "long") == "long"
    gain = (peak - entry) if long_side else (entry - peak)
    reached = gain >= r
    buffer = entry * (2 * ASSUMED_FEE_BPS + ASSUMED_SLIPPAGE_BPS) / 10_000.0
    below_entry_net = exit_ < entry + buffer if long_side else exit_ > entry - buffer
    drawdown = (peak - exit_) if long_side else (exit_ - peak)
    return True, reached, bool(reached and below_entry_net), drawdown > GIVEBACK_REPORT_R * r


def strategy_of(trade) -> str:
    return getattr(trade, "strategy_id", None) or EMA_ID


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


@dataclass
class Group:
    mode: str
    strategy: str | None = None
    pair: str | None = None
    closed: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    trades_with_fees: int = 0
    with_peak: int = 0  # closed trades with a recorded best price (older trades have none)
    reached_1r: int = 0  # ... whose best price was at least +1 R
    gave_back: int = 0  # ... of those, closed below the entry net of (assumed) costs
    far_below_peak: int = 0  # closed trades that finished more than GIVEBACK_REPORT_R below their best price
    funding: float = 0.0  # signed from the account's side: negative = paid
    trades_with_funding: int = 0  # trades whose funding is KNOWN (read, or a Spot trade, which has none)

    @property
    def fees_complete(self) -> bool:
        return self.mode != MODE_PAPER and self.closed > 0 and self.trades_with_fees == self.closed

    @property
    def funding_complete(self) -> bool:
        return self.mode != MODE_PAPER and self.closed > 0 and self.trades_with_funding == self.closed

    @property
    def net_pnl(self) -> float | None:
        """Gross minus fees plus funding, only when every trade in the group has both fee numbers AND its funding known. Never for a practice (simulated) group."""
        return round(self.gross_pnl - self.fees + self.funding, 8) if (self.fees_complete and self.funding_complete) else None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "strategy": self.strategy, "pair": self.pair, "closed_trades": self.closed, "wins": self.wins, "losses": self.losses,
            "gross_pnl": round(self.gross_pnl, 8), "fees": round(self.fees, 8) if self.trades_with_fees else None, "trades_with_fees": self.trades_with_fees,
            "fees_complete": self.fees_complete, "funding": round(self.funding, 8) if self.trades_with_funding else None, "trades_with_funding": self.trades_with_funding,
            "funding_complete": self.funding_complete, "net_pnl": self.net_pnl, "simulated": self.mode == MODE_PAPER,
            "trades_with_peak": self.with_peak, "reached_1r": self.reached_1r, "gave_back_to_loss": self.gave_back, "give_back_rate": round(self.gave_back / self.reached_1r, 4) if self.reached_1r else None,
            "closed_far_below_peak": self.far_below_peak,
        }


def _add(group: Group, trade) -> None:
    pnl = _num(getattr(trade, "pnl", None))
    if pnl is None:
        return  # a closed trade with no readable result is not counted (it appears in no total)
    group.closed += 1
    group.gross_pnl += pnl
    group.wins += 1 if pnl > 0 else 0
    group.losses += 1 if pnl < 0 else 0
    entry, exit_ = _num(getattr(trade, "entry_fee_quote", None)), _num(getattr(trade, "exit_fee_quote", None))
    if entry is not None and exit_ is not None and group.mode != MODE_PAPER:
        group.fees += entry + exit_
        group.trades_with_fees += 1
    known, reached, gave, far = giveback_flags(trade)
    group.with_peak += 1 if known else 0
    group.reached_1r += 1 if reached else 0
    group.gave_back += 1 if gave else 0
    group.far_below_peak += 1 if far else 0
    if group.mode != MODE_PAPER:
        funding = _num(getattr(trade, "funding_quote", None))
        if funding is not None:
            group.funding += funding
            group.trades_with_funding += 1
        elif not funding_applies(trade):
            group.trades_with_funding += 1  # a Spot trade has no funding: known, zero


def build(trades, *, closed_status: str = "closed") -> dict:
    """The report for one account's trades: totals per mode, then per mode and strategy, then per mode, strategy and pair. Only closed trades count."""
    totals: dict[str, Group] = {}
    by_strategy: dict[tuple, Group] = {}
    by_pair: dict[tuple, Group] = {}
    for t in trades:
        if getattr(t, "status", None) != closed_status:
            continue
        mode, strat, pair = mode_of(t), strategy_of(t), getattr(t, "pair", None) or "unknown"
        _add(totals.setdefault(mode, Group(mode)), t)
        _add(by_strategy.setdefault((mode, strat), Group(mode, strat)), t)
        _add(by_pair.setdefault((mode, strat, pair), Group(mode, strat, pair)), t)
    order = [MODE_LIVE, MODE_DEMO, MODE_PAPER]
    return {
        "funding_note": FUNDING_NOTE,
        "modes": [
            {
                "mode": m, "label": MODE_LABEL[m], "total": totals[m].to_dict(),
                "by_strategy": [g.to_dict() for k, g in sorted(by_strategy.items()) if k[0] == m],
                "by_pair": [g.to_dict() for k, g in sorted(by_pair.items()) if k[0] == m],
            }
            for m in order if m in totals
        ],
    }


# ------------------------------------------------------------------------------------------------ the daily Telegram summary
def daily_summary_text(today: str, closed_today, quote: str) -> str:
    """The daily summary with the three modes kept apart. A mode's figure says "net ... after fees" ONLY when every trade of that mode that closed today has both real fee numbers; otherwise it says
    "before fees". Practice is always "simulated". Numbers and fixed words only."""
    groups: dict[str, Group] = {}
    for t in closed_today:
        _add(groups.setdefault(mode_of(t), Group(mode_of(t))), t)
    lines = [f"Daily summary ({today} UTC)"]
    names = {MODE_LIVE: "Live", MODE_DEMO: "Demo (no real money)", MODE_PAPER: "Practice"}
    for m in (MODE_LIVE, MODE_DEMO, MODE_PAPER):
        g = groups.get(m)
        if g is None or g.closed == 0:
            continue
        trades_text = f"{g.closed} trade{'s' if g.closed != 1 else ''} closed"
        if m == MODE_PAPER:
            lines.append(f"{names[m]}: {trades_text}, {g.gross_pnl:+.2f} {quote} (simulated, no fees or slippage)")
        elif g.fees_complete and g.funding_complete:
            lines.append(f"{names[m]}: {trades_text}, net {g.net_pnl:+.2f} {quote} after fees and funding (gross {g.gross_pnl:+.2f}, fees {g.fees:.2f}, funding {g.funding:+.2f})")
        elif g.fees_complete:
            known = f", funding known for {g.trades_with_funding} of {g.closed}" if g.trades_with_funding else ""
            lines.append(f"{names[m]}: {trades_text}, {g.gross_pnl - g.fees:+.2f} {quote} after fees, before funding (gross {g.gross_pnl:+.2f}, fees {g.fees:.2f}){known}")
        else:
            known = f", fees known for {g.trades_with_fees} of {g.closed}" if g.trades_with_fees else ""
            lines.append(f"{names[m]}: {trades_text}, {g.gross_pnl:+.2f} {quote} before fees{known}")
    return "\n".join(lines)
