"""Risk per trade up to 40 percent, and the safety limits that scale with it (docs/safety/risk_per_trade.md).

One place for the numbers and the bands, the same for every account. Pure functions only.

  * Range: a customer may choose risk per trade from RISK_MIN (0.1) up to the current RISK_PCT_MAX (default 2.0, code ceiling 40.0, set by env).
  * Bands: up to and including 2 percent is the recommended band, above 2 and below 10 is "high", 10 and above is "very high". A setting above 2 percent needs a recorded acknowledgment, and a setting from
    10 percent up needs a second one (the server checks them; lowering back to 2 or below needs none).
  * Total loss stop: an account whose equity is TOTAL_STOP_PCT (50) percent or more below its reference equity takes no NEW live entry until the customer resets it. One fixed number for
    every account; it is not a setting and does not depend on the risk per trade.
  * Open-risk cap: `multi_trade.open_risk_cap` (max(MAX_TOTAL_RISK_PCT, risk x the positions the account may hold), never above 100; 0 keeps meaning "no cap").

The DAILY loss limit is the customer's own dollar amount (daily_limit.py); it must stay below the dollar level of the total stop."""
from __future__ import annotations

import math

RISK_MIN = 0.1
RISK_CEILING = 40.0  # a hard clamp on RISK_PCT_MAX, whatever the environment says
RISK_RECOMMENDED_MAX = 2.0
RISK_VERY_HIGH_FROM = 10.0

BAND_RECOMMENDED = "recommended"
BAND_HIGH = "high"
BAND_VERY_HIGH = "very_high"

# Document types and version for the recorded acknowledgments (legal_acceptances, no new table: document_type is a 20-character string).
ACK_HIGH = "risk_ack_high"
ACK_VERY_HIGH = "risk_ack_very_high"
ACK_VERSION = "2026-11-17"

TOTAL_STOP_PCT = 50.0  # the automatic total loss stop: percent below the reference value. One named constant, the same for every account.

_EPS = 1e-9


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def clamp_limit(raw) -> float:
    """RISK_PCT_MAX as configured: never below RISK_MIN, never above the 40 percent code ceiling. A non-number is the default 2.0."""
    if not _finite(raw):
        return 2.0
    return min(RISK_CEILING, max(RISK_MIN, float(raw)))


def band(risk_pct) -> str:
    """recommended (<= 2), high (> 2 and < 10), very_high (>= 10). A non-finite value is very_high (the most cautious reading)."""
    if not _finite(risk_pct):
        return BAND_VERY_HIGH
    if risk_pct <= RISK_RECOMMENDED_MAX + _EPS:
        return BAND_RECOMMENDED
    if risk_pct < RISK_VERY_HIGH_FROM - _EPS:
        return BAND_HIGH
    return BAND_VERY_HIGH


def _rank(b: str) -> int:
    return {BAND_RECOMMENDED: 0, BAND_HIGH: 1, BAND_VERY_HIGH: 2}[b]


def required_acknowledgments(risk_pct) -> tuple[str, ...]:
    """The acknowledgment document types a risk setting needs: none up to 2 percent, the first above 2, both from 10 percent up."""
    b = band(risk_pct)
    return () if b == BAND_RECOMMENDED else (ACK_HIGH,) if b == BAND_HIGH else (ACK_HIGH, ACK_VERY_HIGH)


def validate_range(risk_pct, limit: float) -> str | None:
    """A plain customer-safe problem text, or None if risk_pct is a number from 0.1 up to the limit (boundaries included, float noise tolerated)."""
    if not _finite(risk_pct):
        return "Risk per trade must be a number."
    if risk_pct < RISK_MIN - _EPS:
        return f"Risk per trade must be at least {RISK_MIN:g}%."
    if risk_pct > limit + _EPS:
        return f"Risk per trade can be at most {limit:g}% on this service. Nothing was changed."
    return None


def total_loss_threshold(risk_pct=None) -> float:
    """Percent below the reference equity at which new entries stop: TOTAL_STOP_PCT for every account. `risk_pct` is accepted only so older callers keep working; it is ignored."""
    return TOTAL_STOP_PCT


def total_loss_hit(*, reference_equity, equity, risk_pct=None) -> float | None:
    """The loss in percent if the stop must act, else None. Fail closed: a non-finite or non-positive reference or a non-finite equity is a hit (returns nan)."""
    if not (_finite(reference_equity) and reference_equity > 0 and _finite(equity)):
        return float("nan")
    loss = (reference_equity - equity) / reference_equity * 100.0
    return loss if loss >= TOTAL_STOP_PCT - _EPS else None


def ack_problem_text(doc_type: str) -> str:
    what = "above 2%" if doc_type == ACK_HIGH else "of 10% or more"
    return f"A risk per trade {what} needs your confirmation that you understand the risk. Tick the confirmation on the screen and save again. Nothing was changed."


def risk_setting_problem(risk_pct, limit: float, *, on_file: dict, ticked: dict) -> tuple[str | None, list[str]]:
    """The ONE rule for a customer's risk per trade, used by the account API (which refuses with it) and by the setup advisor (which reports it): (a plain problem text or None, the acknowledgment types
    ticked now that are not on file yet). `on_file` and `ticked` map ACK_HIGH and ACK_VERY_HIGH to booleans."""
    problem = validate_range(risk_pct, limit)
    if problem is not None:
        return problem, []
    to_record: list[str] = []
    for doc_type in required_acknowledgments(risk_pct):
        if on_file.get(doc_type):
            continue
        if not ticked.get(doc_type):
            return ack_problem_text(doc_type), []
        to_record.append(doc_type)
    return None, to_record
