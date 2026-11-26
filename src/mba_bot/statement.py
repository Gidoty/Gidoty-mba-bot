"""A customer's trade statement as CSV (docs/safety/statements_and_closure.md). One row per closed trade with gross, fees, funding and net as SEPARATE columns, and a note saying what is missing and why.

What is filled today (main): gross is `trades.pnl` (before fees and before Futures funding); fees are the real commissions captured from the fills (entry plus exit), filled only when both are known, and never for a
practice (simulated) trade; funding is NOT recorded anywhere yet, so that column is always empty; net is gross minus fees, filled only when both fees are known. Once the net P&L work that records funding and a
net figure per trade (PR #98) is merged, its columns are the ones to add here; until then those cells stay empty and the note says so. Nothing is estimated or invented.

Spreadsheet safety: a text cell that starts with = + - @ tab or return is prefixed with an apostrophe so a spreadsheet cannot run it as a formula. Numbers are written as plain numbers."""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime

from . import reporting

COLUMNS = ("trade_ref", "mode", "pair", "side", "strategy", "opened_at", "closed_at", "entry_price", "exit_price", "quantity", "close_reason", "currency", "gross_pnl", "entry_fee", "exit_fee", "fees", "funding", "net_pnl", "note")
MAX_ROWS = 20000
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _fmt(x) -> str:
    return "" if x is None else f"{x:.8f}".rstrip("0").rstrip(".") or "0"


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else ""


def row_for(trade) -> dict:
    mode = reporting.mode_of(trade)
    gross = _num(getattr(trade, "pnl", None))
    entry_fee, exit_fee = _num(getattr(trade, "entry_fee_quote", None)), _num(getattr(trade, "exit_fee_quote", None))
    fees_known = mode != reporting.MODE_PAPER and entry_fee is not None and exit_fee is not None
    fees = (entry_fee + exit_fee) if fees_known else None
    net = (gross - fees) if (fees_known and gross is not None) else None
    notes = []
    if mode == reporting.MODE_PAPER:
        notes.append("practice trade: simulated, no fees or slippage")
    elif not fees_known:
        notes.append("fees not captured for this trade")
    notes.append("funding is not recorded")
    pair = getattr(trade, "pair", None) or ""
    return {
        "trade_ref": (getattr(trade, "id", "") or "")[:8], "mode": mode, "pair": safe_text(pair), "side": safe_text(getattr(trade, "side", "")), "strategy": safe_text(reporting.strategy_of(trade)),
        "opened_at": _iso(getattr(trade, "opened_at", None)), "closed_at": _iso(getattr(trade, "closed_at", None)), "entry_price": _fmt(_num(getattr(trade, "entry_price", None))),
        "exit_price": _fmt(_num(getattr(trade, "exit_price", None))), "quantity": _fmt(_num(getattr(trade, "quantity", None))), "close_reason": safe_text(getattr(trade, "close_reason", "")),
        "currency": safe_text(pair.split("/")[1] if "/" in pair else ""), "gross_pnl": _fmt(gross), "entry_fee": _fmt(entry_fee if fees_known else None), "exit_fee": _fmt(exit_fee if fees_known else None),
        "fees": _fmt(fees), "funding": "", "net_pnl": _fmt(net), "note": "; ".join(notes),
    }


def build_csv(trades, *, mode: str | None = None, start: datetime | None = None, end: datetime | None = None) -> tuple[str, bool]:
    """(the CSV text, whether it was cut at MAX_ROWS). Closed trades only, oldest first, optionally one mode and a closing-time range (start inclusive, end exclusive)."""
    rows = []
    for t in sorted((t for t in trades if getattr(t, "status", None) == "closed"), key=lambda t: (getattr(t, "closed_at", None) or datetime.min.replace(tzinfo=None), getattr(t, "id", ""))):
        if mode and reporting.mode_of(t) != mode:
            continue
        closed = getattr(t, "closed_at", None)
        if closed is not None and (start or end):
            c = closed.replace(tzinfo=None)
            if (start and c < start.replace(tzinfo=None)) or (end and c >= end.replace(tzinfo=None)):
                continue
        elif closed is None and (start or end):
            continue
        rows.append(row_for(t))
    truncated = len(rows) > MAX_ROWS
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows[:MAX_ROWS])
    return out.getvalue(), truncated
