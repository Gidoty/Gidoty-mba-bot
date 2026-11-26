"""Password hashing and session JWTs.

Kept deliberately small: bcrypt for passwords (via passlib), HS256 JWTs
for sessions, carried in an httpOnly cookie by the auth router. No
refresh-token dance for v1 - tokens just expire after
`JWT_EXPIRES_MINUTES` and the customer logs in again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

SESSION_COOKIE_NAME = "mba_session"

# bcrypt's underlying algorithm only uses the first 72 bytes of the
# input - the schema caps signup passwords at that length too (see
# api/schemas.py) so this is a backstop, not the primary limit.
_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    truncated = password.encode("utf-8")[:_MAX_PASSWORD_BYTES]
    return bcrypt.checkpw(truncated, password_hash.encode("utf-8"))


class TokenError(ValueError):
    pass


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_micros(moment: datetime) -> int:
    """A moment as whole microseconds since 1970 (exact integer arithmetic; a naive value is read as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(microseconds=1)


def create_session_token(*, customer_id: str, secret: str, algorithm: str, expires_minutes: int) -> str:
    now = datetime.now(timezone.utc)
    # `iat` is whole seconds (a JWT standard); `iat_us` is the same moment in microseconds, so "issued before or after a password reset" is exact even within one second.
    payload = {"sub": customer_id, "iat": now, "iat_us": to_micros(now), "exp": now + timedelta(minutes=expires_minutes)}
    return jwt.encode(payload, secret, algorithm=algorithm)


def read_session(token: str, *, secret: str, algorithm: str) -> tuple[str, int]:
    """(customer id, issued-at in microseconds). A token from before `iat_us` existed has only whole seconds, rounded down: that can only make an old token look older, never newer."""
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
    except JWTError as exc:
        raise TokenError("invalid or expired session token") from exc
    customer_id = payload.get("sub")
    if not customer_id:
        raise TokenError("session token missing subject")
    issued = payload.get("iat_us")
    if not isinstance(issued, int) or isinstance(issued, bool):
        iat = payload.get("iat")
        issued = int(iat) * 1_000_000 if isinstance(iat, (int, float)) and not isinstance(iat, bool) else 0
    return customer_id, issued


def read_customer_id(token: str, *, secret: str, algorithm: str) -> str:
    return read_session(token, secret=secret, algorithm=algorithm)[0]
