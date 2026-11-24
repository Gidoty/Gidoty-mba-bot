"""Legal document versions and the single source of truth for whether a
customer's on-file acceptance of each document is still current.

A "version" is just the document's own Effective Date - there's no
separate version-numbering scheme to keep in sync by hand. Bumping a
version here means the document text changed and every customer (new
or existing) needs to accept it again; nothing here ever reuses an old
acceptance row for a new version, and nothing here ever fabricates an
acceptance that didn't happen (see the migration that introduces
`LegalAcceptance` - it adds an empty table, not backfilled rows).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LegalAcceptance, TradingAccount

# The version of each document is its own effective date. Bumping one means the text changed: every customer must accept it again (nothing reuses an old row). The matching `effectiveDate` in
# frontend/src/legalContent.js must change with it (a test checks the two agree).
TERMS_VERSION = "2026-11-23"
PRIVACY_VERSION = "2026-11-21"
RISK_DISCLOSURE_VERSION = "2026-11-21"

# Who operates the service and how to reach them. One place for the code (refusal messages, data-request replies); the legal texts in the frontend repeat it and a test checks they agree.
OPERATOR_NAME = "Metabridge Academy"
OPERATOR_WEBSITE = "metabridgeacademy.com"
OPERATOR_EMAIL = "info@metabridgeacademy.com"

DOCUMENT_TYPES = ("terms", "privacy", "risk_disclosure", "risk_ack_high", "risk_ack_very_high")  # the last two: the risk-per-trade acknowledgments (risk_limits.py)

# The exact, required wording for the Enable Live Trading confirmation -
# shown verbatim and prominently by the frontend, not paraphrased.
LIVE_TRADING_RISK_SENTENCE = (
    "You are enabling LIVE automated cryptocurrency trading. Real orders "
    "may be submitted to your connected Binance account using real funds. "
    "Trading can result in substantial financial loss."
)


def has_current_acceptance(db: Session, *, customer_id: str, document_type: str, version: str) -> bool:
    return (
        db.scalar(
            select(LegalAcceptance.id).where(
                LegalAcceptance.customer_id == customer_id,
                LegalAcceptance.document_type == document_type,
                LegalAcceptance.version == version,
            )
        )
        is not None
    )


def has_current_terms_and_privacy(db: Session, *, customer_id: str) -> bool:
    """The gate every /api/account/* route checks: has this customer
    affirmatively accepted the CURRENT Terms and Privacy Policy. An
    existing customer who signed up before these documents existed has
    zero rows for either and therefore fails this, exactly like a brand
    new customer who tries to skip the checkboxes - there is no
    "grandfathered in" case."""
    return has_current_acceptance(
        db, customer_id=customer_id, document_type="terms", version=TERMS_VERSION
    ) and has_current_acceptance(db, customer_id=customer_id, document_type="privacy", version=PRIVACY_VERSION)


def record_acceptance(
    db: Session, *, customer_id: str, document_type: str, version: str, now: datetime | None = None
) -> LegalAcceptance:
    """Writes one acceptance row. Never called speculatively - every
    caller checks its own preconditions first (signup only calls this
    after the customer row itself is created; enable-live-trading only
    calls this after all four trading-safety gates have already passed),
    so a row here always means the thing it records actually happened."""
    acceptance = LegalAcceptance(
        customer_id=customer_id,
        document_type=document_type,
        version=version,
        accepted_at=now or datetime.now(timezone.utc),
    )
    db.add(acceptance)
    return acceptance


def risk_acceptance_needed(db: Session, *, customer_id: str) -> bool:
    """True for a customer who has ALREADY enabled live trading but has not accepted the CURRENT Risk Disclosure version (their acceptance is of an older version, or missing). A customer who has not
    enabled live trading is not asked here: they accept it at the moment they enable live trading (api/routers/account.py), on the version shown then. Reads only; the trading engine never calls this."""
    live = db.scalar(
        select(TradingAccount.id).where(TradingAccount.customer_id == customer_id, TradingAccount.live_trading_enabled_at.is_not(None)).limit(1)
    )
    if live is None:
        return False
    return not has_current_acceptance(db, customer_id=customer_id, document_type="risk_disclosure", version=RISK_DISCLOSURE_VERSION)


def legal_acceptance_current(db: Session, *, customer_id: str) -> bool:
    """What the screens and the /api/account gate use: Terms and Privacy current, and the Risk Disclosure current too for a customer who has live trading enabled."""
    return has_current_terms_and_privacy(db, customer_id=customer_id) and not risk_acceptance_needed(db, customer_id=customer_id)
