"""Short human-readable identifier for a trading account.

Not a secret and not used for auth or lookup security - just something a
customer can read out over WhatsApp or Telegram when they contact support,
instead of a UUID. Looks like "MBA-7F3K9Q".
"""
from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import TradingAccount

# Excludes 0/O and 1/I - the two pairs people misread or mistype most
# often when copying a code off a screen or reading it aloud.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 6
_MAX_ATTEMPTS = 10


def generate_bot_code() -> str:
    return "MBA-" + "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))


def assign_unique_bot_code(session: Session) -> str:
    """A pre-check query rather than insert-and-catch-IntegrityError, so a
    collision never leaves the caller's transaction needing a rollback."""
    for _ in range(_MAX_ATTEMPTS):
        code = generate_bot_code()
        exists = session.scalar(select(TradingAccount.id).where(TradingAccount.bot_code == code))
        if exists is None:
            return code
    raise RuntimeError("could not generate a unique bot code after several attempts")
