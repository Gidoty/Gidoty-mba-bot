"""Risk per trade up to 40 percent, and the safety limits that scale with it (docs/safety/risk_per_trade.md).

One place for the numbers and the bands, the same for every account. Pure functions only.

  * Range: a customer may choose risk per trade from RISK_MIN (0.1) up to the current RISK_PCT_MAX (default 2.0, code ceiling 40.0, set by env).
  * Bands: up to and including 2 percent is the recommended band, above 2 and below 10 is "high", 10 and above is "very high". A setting above 2 percent needs a recorded acknowledgment, and a setting from
    10 percent up needs a second one (the server checks them; lowering back to 2 or below needs none).
  * Total loss stop: an account whose equity is down max(10, 3 x risk) percent (never above 80) from its reference equity takes no NEW live entry until the customer resets it.
  * Open-risk cap: `multi_trade.open_risk_cap` (max(MAX_TOTAL_RISK_PCT, risk x the positions the account may hold), never above 100; 0 keeps meaning "no cap").

The DAILY loss pause does NOT scale with risk: ACCOUNT_DAILY_LOSS_PCT is one fixed value for every account (account_loss.py is not touched by this module)."""
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

TOTAL_STOP_MIN_PCT = 10.0
TOTAL_STOP_MULTIPLE = 3.0
TOTAL_STOP_CAP_PCT = 80.0

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


def total_loss_threshold(risk_pct) -> float:
    """Percent down from the reference equity at which new entries stop: max(10, 3 x risk), never above 80. A non-finite risk gives the cap (most cautious is the
    smallest threshold, but an unknown risk must not make the stop trigger on noise, so 80 is not used: the base 10 is)."""
    if not _finite(risk_pct) or risk_pct <= 0:
        return TOTAL_STOP_MIN_PCT
    return min(TOTAL_STOP_CAP_PCT, max(TOTAL_STOP_MIN_PCT, TOTAL_STOP_MULTIPLE * float(risk_pct)))


def total_loss_hit(*, reference_equity, equity, risk_pct) -> float | None:
    """The loss in percent if the stop must act, else None. Fail closed: a non-finite or non-positive reference or a non-finite equity is a hit (returns nan)."""
    if not (_finite(reference_equity) and reference_equity > 0 and _finite(equity)):
        return float("nan")
    loss = (reference_equity - equity) / reference_equity * 100.0
    return loss if loss >= total_loss_threshold(risk_pct) - _EPS else None
