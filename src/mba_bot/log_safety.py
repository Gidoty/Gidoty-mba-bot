"""Keeping secrets out of ordinary logs.

The primary defence is never to put a secret where a log can see it (e.g.
TelegramNotifier turns every failure into a sanitized exception whose text
and chain contain no URL). This module is the second layer:

* `quiet_http_client_logs` - httpx logs every request at INFO as
  `HTTP Request: POST <full url>`, and Telegram's Bot API puts the bot token
  IN the URL path, so under INFO logging every successful send would write
  the token to the log. Library request logging is pinned to WARNING.
* `RedactingFilter` / `install_log_safety` - last-line redaction on the
  process's log handlers: the configured secrets (Telegram, WhatsApp,
  Paystack, JWT, encryption and Binance testnet keys) are replaced wherever
  they appear in a record's message or traceback, and anything shaped like a
  Telegram bot URL token is replaced even if unknown to us.
"""
from __future__ import annotations

import logging
import re
import traceback

REDACTED = "<redacted>"
_TELEGRAM_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")
_MIN_SECRET_LENGTH = 8  # shorter values would redact innocuous text


def secrets_from_settings(settings) -> list[str]:
    names = (
        "telegram_bot_token",
        "whatsapp_provider_token",
        "paystack_secret_key",
        "jwt_secret",
        "encryption_key",
        "testnet_spot_api_key",
        "testnet_spot_api_secret",
        "testnet_futures_api_key",
        "testnet_futures_api_secret",
    )
    values = [getattr(settings, name, None) for name in names]
    return [v for v in values if isinstance(v, str) and len(v) >= _MIN_SECRET_LENGTH]


def redact(text: str, secrets=()) -> str:
    # Longest first so a secret that contains another is replaced whole.
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_LENGTH}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return _TELEGRAM_BOT_TOKEN.sub("bot" + REDACTED, text)


class RedactingFilter(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self._secrets = [s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_LENGTH]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message, self._secrets)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Pre-render and redact the traceback; Formatter reuses exc_text.
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip("\n"), self._secrets)
        elif record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self._secrets)
        return True


def quiet_http_client_logs() -> None:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def install_log_safety(settings=None) -> None:
    """Idempotent. Call once the logging handlers exist."""
    quiet_http_client_logs()
    secrets = secrets_from_settings(settings) if settings is not None else []
    loggers = [logging.getLogger(), *(logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access"))]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter(secrets))
