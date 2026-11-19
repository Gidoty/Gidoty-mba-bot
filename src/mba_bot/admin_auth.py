"""Admin sign-in (docs/security/admin_dashboard.md): who is an admin, the TOTP second factor, recovery codes and the short admin session.

Who is an admin: a customer whose email is in the ADMIN_EMAILS environment list. Never a database column, so editing the database cannot create an admin. Removing an email from the list ends that
admin's access on their next request.

How they prove it: the normal sign-in (password) first, then a 6-digit code from an authenticator app (or a recovery code). A code is a 30-second time step with one step of drift either side, and a step
can be used once (`last_step`). Five wrong codes lock the admin sign-in for 15 minutes. The secret is encrypted with the same Fernet key as the exchange credentials. Recovery codes are stored hashed and are
removed when used. The admin session is a separate cookie, signed with a key derived from JWT_SECRET so a normal session token is never an admin token, and it lasts 15 minutes from the last request.

Nothing here reads a request header: the address used for rate limiting is the one the server already resolved (`request.client.host`)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp
from jose import JWTError, jwt
from sqlalchemy import update
from sqlalchemy.orm import Session

from . import crypto_utils
from .models import AdminTotp

ADMIN_COOKIE_NAME = "mba_admin"
STEP_SECONDS = 30
DRIFT_STEPS = 1
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_MINUTES = 15
RECOVERY_CODE_COUNT = 10
ISSUER = "MBA Bot"

R_OK, R_BAD, R_LOCKED, R_NOT_ENROLLED, R_REPLAY = "ok", "bad_code", "locked", "not_enrolled", "code_already_used"
_CODE_RE = re.compile(r"^[0-9]{6}$")
_RECOVERY_RE = re.compile(r"^[A-Z2-7]{16}$")


def is_admin_email(email: str | None, admin_emails) -> bool:
    return bool(email) and email.strip().lower() in set(admin_emails or ())


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ TOTP
def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret, interval=STEP_SECONDS).provisioning_uri(name=account_name, issuer_name=ISSUER)


def normalise_code(raw) -> str:
    return re.sub(r"[\s-]", "", raw).upper() if isinstance(raw, str) else ""


def matching_step(secret: str, code: str, *, now_ts: float) -> int | None:
    """The 30-second time step whose code equals `code` (this step or one either side), else None. Compared in constant time against every step so the answer does not depend on which one matched."""
    if not _CODE_RE.match(code or ""):
        return None
    totp = pyotp.TOTP(secret, interval=STEP_SECONDS)
    current = int(now_ts // STEP_SECONDS)
    found = None
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), code):
            found = step
    return found


# ------------------------------------------------------------------------------------------------ recovery codes
def _digest(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> tuple[list[str], list[str]]:
    """(codes to show the owner once, the stored form). A code is 16 base32 characters (80 bits); the store holds a random salt and a SHA-256 digest of it, never the code."""
    plain, stored = [], []
    for _ in range(count):
        code = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
        salt = secrets.token_hex(8)
        plain.append(code)
        stored.append(f"{salt}${_digest(salt, code)}")
    return plain, stored


def find_recovery(code: str, stored: list[str]) -> int | None:
    if not _RECOVERY_RE.match(code or ""):
        return None
    found = None
    for i, item in enumerate(stored):
        salt, _, digest = item.partition("$")
        if hmac.compare_digest(_digest(salt, code), digest):
            found = i
    return found


# ------------------------------------------------------------------------------------------------ enrolment (the command-line tool calls this)
def enrol(db: Session, customer_id: str, *, encryption_key: str, replace: bool = False) -> tuple[str, list[str]] | None:
    """Create (or, with replace, reset) the TOTP row. Returns (setup key, recovery codes) to print ONCE, or None when the admin is already enrolled and `replace` is not set."""
    existing = db.get(AdminTotp, customer_id)
    if existing is not None and not replace:
        return None
    secret = new_secret()
    codes, stored = new_recovery_codes()
    row = existing or AdminTotp(customer_id=customer_id)
    row.secret_enc = crypto_utils.encrypt(secret, key=encryption_key)
    row.recovery_hashes = json.dumps(stored)
    row.last_step, row.failures, row.locked_until = 0, 0, None
    row.enrolled_at = datetime.now(timezone.utc)
    db.add(row)
    db.flush()
    return secret, codes


# ------------------------------------------------------------------------------------------------ the sign-in check
@dataclass(frozen=True)
class LoginResult:
    outcome: str
    retry_after: int = 0


def attempt_login(db: Session, customer_id: str, raw_code, *, encryption_key: str, now: datetime | None = None) -> LoginResult:
    """Check one code (6 digits, or a recovery code). Counts failures, locks after five, refuses a replayed time step, and removes a used recovery code. Never reveals which part was wrong."""
    now = now or datetime.now(timezone.utc)
    row = db.get(AdminTotp, customer_id)
    if row is None:
        return LoginResult(R_NOT_ENROLLED)
    locked = _aware(row.locked_until)
    if locked is not None and locked > now:
        return LoginResult(R_LOCKED, int((locked - now).total_seconds()) + 1)
    code = normalise_code(raw_code)
    secret = crypto_utils.decrypt(row.secret_enc, key=encryption_key)
    step = matching_step(secret, code, now_ts=now.timestamp())
    if step is not None:
        # Atomic: only one request can move last_step forward to this value, so the same code cannot be used twice even by two requests at once.
        moved = db.execute(update(AdminTotp).where(AdminTotp.customer_id == customer_id, AdminTotp.last_step < step).values(last_step=step, failures=0, locked_until=None)).rowcount
        if moved == 1:
            return LoginResult(R_OK)
        return _failure(db, row, now, replay=True)
    stored = json.loads(row.recovery_hashes or "[]")
    idx = find_recovery(code, stored)
    if idx is not None:
        remaining = [s for i, s in enumerate(stored) if i != idx]
        moved = db.execute(update(AdminTotp).where(AdminTotp.customer_id == customer_id, AdminTotp.recovery_hashes == row.recovery_hashes).values(recovery_hashes=json.dumps(remaining), failures=0, locked_until=None)).rowcount
        if moved == 1:
            return LoginResult(R_OK)
    return _failure(db, row, now)


def _failure(db: Session, row: AdminTotp, now: datetime, *, replay: bool = False) -> LoginResult:
    db.refresh(row)
    failures = (row.failures or 0) + 1
    if failures >= MAX_FAILURES:
        row.failures, row.locked_until = 0, now + timedelta(seconds=LOCK_SECONDS)
        db.flush()
        return LoginResult(R_LOCKED, LOCK_SECONDS)
    row.failures = failures
    db.flush()
    return LoginResult(R_REPLAY if replay else R_BAD)


# ------------------------------------------------------------------------------------------------ the admin session
def _key(jwt_secret: str) -> str:
    return f"{jwt_secret}|admin-session"


def create_admin_token(*, customer_id: str, jwt_secret: str, algorithm: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return jwt.encode({"sub": customer_id, "typ": "admin", "iat": now, "exp": now + timedelta(minutes=SESSION_MINUTES)}, _key(jwt_secret), algorithm=algorithm)


def read_admin_token(token: str | None, *, jwt_secret: str, algorithm: str) -> str | None:
    """The customer id in a valid, unexpired admin token, else None."""
    if not token or not jwt_secret:
        return None
    try:
        payload = jwt.decode(token, _key(jwt_secret), algorithms=[algorithm])
    except JWTError:
        return None
    return payload.get("sub") if payload.get("typ") == "admin" and payload.get("sub") else None
