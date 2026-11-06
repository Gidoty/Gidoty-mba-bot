#!/usr/bin/env python3
"""Alerts for the backup tools: a Telegram message through a separate ops bot, and an optional dead-man ping.

    mba_alert.py backup_failed <step>

Messages are FIXED text plus a step name from a fixed list; no exception text, path, file content or secret is ever put in a
message. The bot token and chat id come from /etc/mba-backup/alert.env (mode 600), read by a strict parser. Every function
here is best effort and returns False instead of raising: an alert must never hide the failure it reports or change an exit
status. The token sits in the request URL (that is how the Telegram API works), so the URL is never logged or printed, and
failures are reported by category only.

Network targets are fixed: https://api.telegram.org for alerts, and an https URL for the dead-man ping. Plain http is accepted
only for 127.0.0.1 (used by the tests).
"""
from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import mba_common as common

TELEGRAM_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 10


def _safe_base(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname == "127.0.0.1")


def load_alert_config() -> dict[str, str] | None:
    try:
        return common.parse_env_file(os.path.join(common.config_dir(), "alert.env"), common.ALERT_ENV_KEYS, required=("OPS_BOT_TOKEN", "OPS_CHAT_ID"))
    except common.ConfigError:
        return None


def send_telegram(text: str, cfg: dict[str, str] | None = None) -> bool:
    cfg = cfg or load_alert_config()
    if not cfg:
        sys.stderr.write("alert: no usable alert configuration\n")
        return False
    base = os.environ.get("MBA_ALERT_API_BASE", TELEGRAM_BASE)
    if not _safe_base(base):
        sys.stderr.write("alert: refused an unsafe alert endpoint\n")
        return False
    data = urllib.parse.urlencode({"chat_id": cfg["OPS_CHAT_ID"], "text": text[:1000]}).encode()
    request = urllib.request.Request(f"{base}/bot{cfg['OPS_BOT_TOKEN']}/sendMessage", data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - scheme checked above
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 - category only: the message can carry the URL, which holds the token
        sys.stderr.write(f"alert: could not send ({type(exc).__name__})\n")
        return False


def ping_deadman(cfg: dict[str, str] | None = None) -> bool:
    """One GET to the dead-man URL, if one is configured. The URL is a secret of the ping service; never printed."""
    cfg = cfg or load_alert_config()
    url = (cfg or {}).get("DEADMAN_URL")
    if not url:
        return False
    if not _safe_base(url):
        sys.stderr.write("deadman: refused an unsafe URL\n")
        return False
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"deadman: could not ping ({type(exc).__name__})\n")
        return False


def failure_text(step: str, when: float | None = None) -> str:
    step = step if step in common.ALERT_STEPS else "unknown"
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(when if when is not None else time.time()))
    return f"MBA backup FAILED at {stamp}, step: {step}. Read the log on the server: /opt/mba-bot/backups/backup.log and the watchdog log."


def alert_failure(step: str, when: float | None = None) -> bool:
    return send_telegram(failure_text(step, when))


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "backup_failed":
        alert_failure(argv[2])
        return 0  # never alters anything: the caller already has its own exit status
    sys.stderr.write("usage: mba_alert.py backup_failed <step>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
