"""Per-account results reporting (docs/safety/reporting.md). Ships behind REPORTING_V2 (default off). Pure functions: no I/O, no clock, no database.

One account's CLOSED trades are grouped by mode (live, demo, practice), strategy and pair. Paper, demo and live are never added together. For each group:
  * gross P&L is the sum of `trades.pnl` (before exchange fees and before Futures funding);
  * fees are the sum of the real commissions captured from the fills (`entry_fee_quote` + `exit_fee_quote`, PR fee capture). A group is `fees_complete` only if EVERY closed trade in it has both numbers;
  * net P&L (gross - fees) is given ONLY for a complete live or demo group. Otherwise it is None and the wording says "before fees". Futures funding is never included in either figure;
  * practice (paper) results are SIMULATED: no fees, no slippage, never net.
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
FUNDING_NOTE = "Futures funding payments are not included in any figure."


def mode_of(trade) -> str:
    if getattr(trade, "is_demo", False):
        return MODE_DEMO
    return MODE_PAPER if getattr(trade, "is_paper", False) else MODE_LIVE


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

    @property
    def fees_complete(self) -> bool:
        return self.mode != MODE_PAPER and self.closed > 0 and self.trades_with_fees == self.closed

    @property
    def net_pnl(self) -> float | None:
        """Gross minus fees, only when every trade in the group has both fee numbers. Never for a practice (simulated) group."""
        return round(self.gross_pnl - self.fees, 8) if self.fees_complete else None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "strategy": self.strategy, "pair": self.pair, "closed_trades": self.closed, "wins": self.wins, "losses": self.losses,
            "gross_pnl": round(self.gross_pnl, 8), "fees": round(self.fees, 8) if self.trades_with_fees else None, "trades_with_fees": self.trades_with_fees,
            "fees_complete": self.fees_complete, "net_pnl": self.net_pnl, "simulated": self.mode == MODE_PAPER,
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
        elif g.fees_complete:
            lines.append(f"{names[m]}: {trades_text}, net {g.net_pnl:+.2f} {quote} after fees ({g.gross_pnl:+.2f} before fees, fees {g.fees:.2f})")
        else:
            known = f", fees known for {g.trades_with_fees} of {g.closed}" if g.trades_with_fees else ""
            lines.append(f"{names[m]}: {trades_text}, {g.gross_pnl:+.2f} {quote} before fees{known}")
    return "\n".join(lines)
