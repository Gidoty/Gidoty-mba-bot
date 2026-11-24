"""The customer-facing status line (docs/safety/health_and_status.md): one plain sentence about the bot, when it last checked the market, and why it is not trading when it is not. Ships behind STATUS_LINE
(default off: the route answers 404). Pure: no I/O, no clock; the route supplies the facts. It reads what the service already holds and changes nothing.

Words only a customer needs: no error text, no exchange message, no internal state name, no address and no secret ever appears here. `last_error` is a flag, never printed."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STALE_MIN_SECONDS = 15 * 60
STALE_ROUNDS = 3  # a bot is "behind" when it has missed three rounds in a row

RUNNING, PAUSED, WAITING, ON_HOLD, DELAYED, PROBLEM = "running", "paused", "waiting", "on_hold", "delayed", "problem"


@dataclass(frozen=True)
class Facts:
    runtime_status: str  # the account screen's own state: paused | not_started | attention | error | trade_open | active
    needs_telegram: bool
    last_scanned_at: datetime | None
    last_check_ok: bool  # the last check did not end in a problem (the account has no stored error)
    poll_interval_seconds: int
    kill_engaged: bool
    daily_loss_pause: bool
    total_loss_stop: bool
    open_position: bool


def _aware(v: datetime | None) -> datetime | None:
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return "just now"
    if s < 90 * 60:
        return f"{round(s / 60)} minutes ago"
    if s < 36 * 3600:
        return f"{round(s / 3600)} hours ago"
    return f"{round(s / 86400)} days ago"


def stale_after(poll_interval_seconds: int) -> int:
    return max(STALE_MIN_SECONDS, STALE_ROUNDS * max(60, int(poll_interval_seconds)))


def build(f: Facts, now: datetime) -> dict:
    now = _aware(now)
    last = _aware(f.last_scanned_at)
    age = (now - last).total_seconds() if last else None
    stale = age is not None and age > stale_after(f.poll_interval_seconds)
    minutes = max(1, round(f.poll_interval_seconds / 60))
    open_text = " Your open position stays protected by its stop-loss." if f.open_position else ""
    # who is in charge, most specific first
    if f.runtime_status == "paused":
        state, head, detail = PAUSED, "Your bot is paused.", "No new trades are opened. If you did not expect this, contact support."
    elif f.needs_telegram:
        state, head, detail = WAITING, "Your bot is waiting for your Telegram link.", "It does not check the market until your Telegram chat is linked."
    elif f.runtime_status == "not_started":
        state, head, detail = WAITING, "Your bot is ready and waiting for you to press Start Bot.", None
    elif f.runtime_status == "attention":
        state, head, detail = ON_HOLD, "A position needs a safety check.", "The bot is working on it and new trades are on hold until it is resolved."
    elif f.kill_engaged:
        state, head, detail = ON_HOLD, "New trades are paused by the service operator.", "This applies to everyone on the service." + open_text
    elif f.total_loss_stop:
        state, head, detail = ON_HOLD, "New trades are stopped: your account reached its total loss stop.", "You can reset it in your settings after you have looked at your account." + open_text
    elif f.daily_loss_pause:
        state, head, detail = ON_HOLD, "No new trades until tomorrow: your daily loss limit was reached.", open_text.strip() or None
    elif f.runtime_status == "error" or (not f.last_check_ok and f.last_scanned_at is not None):
        state, head, detail = PROBLEM, "The last check ran into a problem.", "The bot tries again on the next round." + open_text
    elif last is None:
        state, head, detail = WAITING, "Your bot has not checked the market yet.", "The first check happens within a few minutes."
    elif stale:
        state, head, detail = DELAYED, f"Your bot has not checked the market since {ago(age)}.", "This is being looked at." + open_text
    elif f.runtime_status == "trade_open" or f.open_position:
        state, head, detail = RUNNING, "Running. A position is open and being watched.", f"The bot checks every {minutes} minutes."
    else:
        state, head, detail = RUNNING, "Running.", f"The bot checks the market every {minutes} minutes."
    return {"state": state, "headline": head, "detail": detail, "last_check_at": last.isoformat() if last else None, "last_check": ago(age) if age is not None else None, "last_check_ok": bool(f.last_check_ok and last is not None)}
