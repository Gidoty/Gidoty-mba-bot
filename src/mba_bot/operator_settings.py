"""Operator settings kept in the database (docs/safety/operator_settings.md): NON-SECRET switches the operator can change from the admin dashboard without editing the environment file.

The rules, in code:
  * An explicit allowlist (ALLOWLIST) says which keys exist, their type, bounds, code default, whether a change needs a restart and whether a customer can ever see the value. Any other key is refused.
  * The value types are a boolean, a bounded number, or one word from a fixed list. There is no free-text type, so a secret (key, token, password, webhook secret, SSH or age material) cannot be typed in. A test fails
    if a key name contains a secret word.
  * `get_setting` reads one value: the database row if there is a valid one, else the environment value (the Settings object). A missing row, a missing table, an unreadable database or a bad stored value all
    give the environment value: the .env keeps working and an empty table changes nothing.
  * `effective_settings` applies the rows to a Settings object. Keys that need a restart are applied only at startup (`startup=True`); the others on the next request or the next scheduler round.
  * Every change is written with an AdminChange row (who, when, old, new, reason) in the same transaction as the value.
Nothing here places, closes or changes a trade or an exchange order."""
from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import AdminChange, OperatorSetting

logger = logging.getLogger(__name__)

SECRET_WORDS = frozenset({"key", "secret", "token", "password", "passwd", "webhook", "ssh", "age", "private", "credential", "credentials", "seed", "mnemonic"})
CACHE_SECONDS = 5.0


@dataclass(frozen=True)
class SettingDef:
    key: str  # also the Settings field it overrides
    kind: str  # bool | int | float | choice
    default: object  # the code default (what the environment gives when nothing is set)
    description: str
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    restart_required: bool = False
    customer_visible: bool = False


ALLOWLIST: dict[str, SettingDef] = {d.key: d for d in (
    SettingDef("signups_mode", "choice", "open", "Whether new customers can sign up.", choices=("open", "closed"), customer_visible=True),
    SettingDef("setup_advisor", "bool", False, "Show the setup advisor notes.", customer_visible=True),
    SettingDef("poll_interval_seconds", "int", 300, "Seconds between scheduler rounds.", lo=60, hi=3600),
    SettingDef("subscription_grace_days", "int", 5, "Days of access after a subscription period ends.", lo=0, hi=30),
    SettingDef("login_max_failures", "int", 5, "Wrong passwords before a sign-in lockout.", lo=3, hi=20),
    SettingDef("login_lockout_seconds", "int", 900, "Seconds a sign-in lockout lasts.", lo=60, hi=86400),
    SettingDef("rate_limit_per_minute", "int", 120, "Requests a minute per address (needs the rate limit switch on).", lo=10, hi=1000, restart_required=True),
    SettingDef("rate_limit_auth_per_minute", "int", 10, "Sign-in requests a minute per address.", lo=3, hi=200, restart_required=True),
    SettingDef("disable_api_docs", "bool", False, "Do not serve the interactive API docs.", restart_required=True),
)}

def validate_allowlist(defs) -> None:
    """Fails at import, not in production, if someone adds a secret-looking key or a free-text type."""
    for d in defs:
        if set(d.key.split("_")) & SECRET_WORDS:
            raise RuntimeError(f"operator setting {d.key!r} looks like a secret and cannot be in the table")
        if d.kind not in ("bool", "int", "float", "choice"):
            raise RuntimeError(f"operator setting {d.key!r} has an unsupported type")


validate_allowlist(ALLOWLIST.values())


class SettingError(ValueError):
    pass


def definition(key: str) -> SettingDef:
    d = ALLOWLIST.get(key) if isinstance(key, str) else None
    if d is None:
        raise SettingError("that setting does not exist")
    return d


def coerce(d: SettingDef, raw) -> object:
    """The typed value for `raw`, or SettingError. Booleans accept true/false/on/off/1/0 and real booleans; numbers must be inside the bounds; a choice must be on the list."""
    if d.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "on", "1", "yes"):
            return True
        if text in ("false", "off", "0", "no"):
            return False
        raise SettingError("use true or false")
    if d.kind in ("int", "float"):
        if isinstance(raw, bool):
            raise SettingError("that is not a number")
        try:
            value = int(str(raw).strip()) if d.kind == "int" else float(str(raw).strip())
        except (TypeError, ValueError):
            raise SettingError("that is not a number") from None
        if value != value or not (d.lo <= value <= d.hi):
            raise SettingError(f"use a number from {d.lo:g} to {d.hi:g}")
        return value
    text = str(raw).strip().lower()
    if text not in d.choices:
        raise SettingError("use one of: " + ", ".join(d.choices))
    return text


def stored(value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


# ------------------------------------------------------------------------------------------------ reading
_cache: dict = {"at": 0.0, "rows": {}, "url": None}


def reset_cache() -> None:
    _cache.update(at=0.0, rows={}, url=None)


def _read_rows(base: Settings, engine=None) -> dict[str, str]:
    from . import db as dbmod

    eng = engine or dbmod._engine
    own = None
    try:
        if eng is None:
            if not str(base.database_url).startswith("sqlite"):
                return {}
            own = eng = create_engine(base.database_url, connect_args={"check_same_thread": False})
        with eng.connect() as conn:
            return {k: v for k, v in conn.execute(select(OperatorSetting.key, OperatorSetting.value)).all()}
    except Exception:  # noqa: BLE001 - a missing table (before the migration), a locked or unreadable database: the environment value is used
        return {}
    finally:
        if own is not None:
            own.dispose()


def rows(base: Settings, *, engine=None, fresh: bool = False) -> dict[str, str]:
    now = time.monotonic()
    if not fresh and engine is None and _cache["url"] == base.database_url and now - _cache["at"] < CACHE_SECONDS:
        return _cache["rows"]
    data = _read_rows(base, engine)
    if engine is None:
        _cache.update(at=now, rows=data, url=base.database_url)
    return data


def valid_overrides(base: Settings, *, startup: bool, engine=None, fresh: bool = False) -> dict[str, object]:
    """{settings field: typed value} for every stored row that is on the allowlist, valid, and (unless `startup`) does not need a restart."""
    out: dict[str, object] = {}
    for key, raw in rows(base, engine=engine, fresh=fresh).items():
        d = ALLOWLIST.get(key)
        if d is None or (d.restart_required and not startup):
            continue
        try:
            out[key] = coerce(d, raw)
        except SettingError:
            logger.warning("operator setting %s has an invalid stored value; the environment value is used", key)
    return out


def effective_settings(base: Settings, *, startup: bool = False, engine=None) -> Settings:
    """`base` (the environment) with the database rows applied. Never raises; with no rows it returns `base` itself."""
    try:
        over = valid_overrides(base, startup=startup, engine=engine)
        return dataclasses.replace(base, **over) if over else base
    except Exception:  # noqa: BLE001
        logger.exception("operator settings could not be applied; the environment values are used")
        return base


def get_setting(key: str, base: Settings, *, engine=None, startup: bool = False):
    """THE one way to read an allowlisted setting: the database value if there is a valid one, else the environment value from `base`."""
    d = definition(key)
    over = valid_overrides(base, startup=startup or d.restart_required, engine=engine)
    return over[key] if key in over else getattr(base, d.key)


def listing(base: Settings, *, engine=None) -> list[dict]:
    """For the dashboard: every allowlisted key with its bounds, its environment value, the stored value, the value in effect and where it comes from."""
    stored_rows = rows(base, engine=engine, fresh=True)
    out = []
    for d in ALLOWLIST.values():
        env_value = getattr(base, d.key)
        item = {"key": d.key, "type": d.kind, "description": d.description, "default": d.default, "min": d.lo, "max": d.hi, "choices": list(d.choices), "restart_required": d.restart_required,
                "customer_visible": d.customer_visible, "environment_value": env_value, "stored_value": stored_rows.get(d.key), "source": "environment"}
        item["effective_value"] = env_value
        if d.key in stored_rows:
            try:
                item["effective_value"] = coerce(d, stored_rows[d.key])
                item["source"] = "database"
                if d.restart_required:
                    item["note"] = "stored; takes effect at the next restart"
                    item["effective_value"] = env_value
            except SettingError:
                item["note"] = "stored value is invalid and ignored"
        out.append(item)
    return out


def customer_view(base: Settings, *, engine=None) -> dict:
    """Only the settings a customer may see, with their effective values. Used by customer-facing screens; nothing else is ever returned."""
    return {k: get_setting(k, base, engine=engine) for k, d in ALLOWLIST.items() if d.customer_visible}


# ------------------------------------------------------------------------------------------------ writing (the caller commits)
def _clean_reason(reason) -> str:
    text = " ".join(str(reason or "").split())
    if not (5 <= len(text) <= 200):
        raise SettingError("give a reason of 5 to 200 characters")
    return text


def set_value(db: Session, *, admin_id: str, key: str, raw, reason: str, base: Settings, now: datetime | None = None) -> AdminChange:
    d = definition(key)
    value = coerce(d, raw)
    text = _clean_reason(reason)
    now = now or datetime.now(timezone.utc)
    row = db.get(OperatorSetting, key)
    old = row.value if row is not None else f"env:{stored(getattr(base, d.key))}"
    if row is None:
        db.add(OperatorSetting(key=key, value=stored(value), updated_at=now, updated_by=admin_id))
    else:
        row.value, row.updated_at, row.updated_by = stored(value), now, admin_id
    change = AdminChange(at=now, admin_id=admin_id, kind="setting", action="set", target=key, old=old[:120], new=stored(value), reason=text)
    db.add(change)
    reset_cache()
    return change


def reset_value(db: Session, *, admin_id: str, key: str, reason: str, base: Settings, now: datetime | None = None) -> AdminChange:
    """Remove the stored row so the environment value applies again."""
    d = definition(key)
    text = _clean_reason(reason)
    now = now or datetime.now(timezone.utc)
    row = db.get(OperatorSetting, key)
    if row is None:
        raise SettingError("nothing is stored for that setting")
    old = row.value
    db.delete(row)
    change = AdminChange(at=now, admin_id=admin_id, kind="setting", action="reset", target=key, old=old, new=f"env:{stored(getattr(base, d.key))}"[:120], reason=text)
    db.add(change)
    reset_cache()
    return change
