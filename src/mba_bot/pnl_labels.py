"""Wording for profit figures. Behind PNL_LABELS (default OFF): with the switch off every string is exactly what it was before.

`trades.pnl` is GROSS: (exit - entry) x quantity from the exchange's reported fills. It contains no exchange fee and no
futures funding. Paper results are a simulation with no fees and no slippage. Docs: designs/06 in the private handoff notes.
Nothing here changes a number, a trade or an order; it only names what a figure is.
"""
from __future__ import annotations

import os

PNL_BASIS = "gross_before_fees"


def enabled() -> bool:
    """Read from the environment on each call (cheap) so the API and the notifications agree without threading settings through."""
    return os.environ.get("PNL_LABELS", "").strip().lower() in ("1", "true", "yes", "on")


def basis() -> str | None:
    """Value of the optional `pnl_basis` API field: set only while the labels are on, so clients show the new wording only then."""
    return PNL_BASIS if enabled() else None


def quote_of(pair: str | None) -> str | None:
    return pair.split("/")[-1] if pair and "/" in pair else None


def exit_line(entry: float, exit_: float, pnl: float, *, on: bool, paper: bool = False, quote: str | None = None) -> str:
    base = f"Entry: {entry:.4f}  Exit: {exit_:.4f}  "
    if not on:
        return base + f"P&L: {pnl:+.2f}"
    unit = f" {quote}" if quote else ""
    if paper:
        return base + f"P&L (simulated, no fees or slippage): {pnl:+.2f}{unit}"
    return base + f"P&L before fees: {pnl:+.2f}{unit}"


def daily_summary(today: str, count: int, total: float, quote: str, *, on: bool, paper_count: int = 0) -> str:
    if not on:
        return f"Daily P&L summary ({today} UTC): {count} trade(s) closed, net {total:+.2f} {quote}"
    if paper_count == count:
        basis_text = "simulated, no fees or slippage"
    elif paper_count == 0:
        basis_text = "before fees"
    else:
        basis_text = "before fees; practice trades are simulated"
    return f"Daily P&L summary ({today} UTC, {basis_text}): {count} trade(s) closed, {total:+.2f} {quote}"
