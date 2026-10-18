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

from .models import LegalAcceptance

TERMS_VERSION = "2026-10-18"
PRIVACY_VERSION = "2026-10-18"
RISK_DISCLOSURE_VERSION = "2026-10-18"

DOCUMENT_TYPES = ("terms", "privacy", "risk_disclosure")

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
