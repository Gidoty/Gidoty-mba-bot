"""Keeping secrets and account data out of ordinary logs.

The primary defence is never to put a secret where a log can see it (e.g.
TelegramNotifier turns every failure into a sanitized exception whose text
and chain contain no URL). This module is the second and third layer:

1. `quiet_http_client_logs` - third-party HTTP clients that carry credentials
   are pinned to WARNING, so the application can run at DEBUG for
   troubleshooting without them:
   * httpx logs `HTTP Request: POST <full url>` at INFO, and Telegram's Bot
     API puts the bot token in the URL path;
   * ccxt logs every request AND response at DEBUG (`ccxt.base.exchange`),
     including the `X-MBX-APIKEY` header (the Binance API key), the signed
     URL (order parameters + signature) and response bodies (balances).
   The `ccxt` logger and every existing `ccxt.*` child get an explicit level,
   so a child logger cannot opt back in; other loggers (the application's
   own) are untouched. Changing these loggers deliberately after start-up is
   outside this guarantee: debug logging of credential-bearing third-party
   HTTP clients must not be enabled in production.

2. A log-record factory (`install_log_safety`) redacts every record at
   creation - before ANY handler, including logging.lastResort, which the
   API deployment relies on - so the message and the rendered traceback never
   carry (record structure is preserved: `args` keeps its type and length so
   structured formatters such as uvicorn's AccessFormatter keep working; see
   `_redact_record`): configured secrets (Telegram, WhatsApp, Paystack, JWT, encryption
   and Binance testnet keys); anything shaped like a Telegram bot-URL token;
   an `X-MBX-APIKEY` header value; or a signed Binance URL (its whole query
   string is dropped - it holds the order parameters, the timestamp and a
   signature that is replayable inside the exchange's recvWindow). Host and
   path are kept, so a failure remains diagnosable.
"""
from __future__ import annotations

import logging
import re
import traceback

REDACTED = "<redacted>"
_TELEGRAM_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")
_API_KEY_HEADER = re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]+", re.IGNORECASE)
_SIGNED_URL = re.compile(r"(https?://[^\s'\"?]+)\?[^\s'\"]*?signature=[0-9a-fA-F]+[^\s'\"]*")
_BARE_SIGNATURE = re.compile(r"signature=[0-9a-fA-F]{16,}")
_MIN_SECRET_LENGTH = 8  # shorter values would redact innocuous text
_THIRD_PARTY_LOGGERS = ("httpx", "httpcore", "ccxt")

_secrets: set[str] = set()
_base_factory = None


def secrets_from_settings(settings) -> list[str]:
    names = (
        "telegram_bot_token",
        "whatsapp_provider_token",
        "paystack_secret_key",
        "jwt_secret",
        "encryption_key",
        "testnet_spot_api_key",
        "testnet_spot_api_secret",
        "demo_futures_api_key",
        "demo_futures_api_secret",
        "telegram_webhook_secret",
    )
    values = [getattr(settings, name, None) for name in names]
    return [v for v in values if isinstance(v, str) and len(v) >= _MIN_SECRET_LENGTH]


def redact(text: str, secrets=()) -> str:
    # Longest first so a secret that contains another is replaced whole.
    usable = {s for s in (*secrets, *_secrets) if isinstance(s, str) and len(s) >= _MIN_SECRET_LENGTH}
    for secret in sorted(usable, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _SIGNED_URL.sub(lambda m: f"{m.group(1)}?{REDACTED}", text)
    text = _BARE_SIGNATURE.sub("signature=" + REDACTED, text)
    text = _API_KEY_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    return _TELEGRAM_BOT_TOKEN.sub("bot" + REDACTED, text)


def _redact_args(args, secrets):
    """Redact string arguments in place of a copy, keeping the container type,
    the length and every non-string value (ints, floats, ...) exactly as they
    were: structured formatters (uvicorn's AccessFormatter unpacks
    `record.args` as five fields) depend on that shape."""
    if isinstance(args, tuple):
        return tuple(redact(a, secrets) if isinstance(a, str) else a for a in args)
    if isinstance(args, dict):
        return {k: (redact(v, secrets) if isinstance(v, str) else v) for k, v in args.items()}
    return args


def _redact_record(record: logging.LogRecord, secrets=()) -> None:
    """Remove secrets from a record WITHOUT changing its structure unless that
    is the only way to remove them.

    1. Nothing to redact (the overwhelmingly common case): the record is left
       exactly as the logger made it.
    2. Something to redact: the message template and each string argument are
       redacted individually, so `record.args` keeps its shape and the
       record's own formatter still works (uvicorn access logs).
    3. If the formatted message STILL differs from the fully-redacted one (a
       secret that only appears once template and arguments are joined, or in
       the str() of a non-string argument), fall back to a flattened record -
       message redacted, args dropped. Secrecy outranks structure here; this
       only happens for free-form messages, never for fixed-template
       structured records such as access logs.
    """
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    redacted = redact(message, secrets)
    if redacted != message:
        if record.args:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg, secrets)
            record.args = _redact_args(record.args, secrets)
            try:
                still = record.getMessage()
            except Exception:
                still = None
            if still != redacted:
                record.msg, record.args = redacted, None
        else:
            record.msg = redacted  # no args: msg already IS the whole message
    if record.exc_info and record.exc_info[1] is not None:
        # Pre-render and redact the traceback; Formatter reuses exc_text.
        record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip("\n"), secrets)
    elif record.exc_text:
        record.exc_text = redact(record.exc_text, secrets)
    if record.stack_info:
        record.stack_info = redact(record.stack_info, secrets)


class RedactingFilter(logging.Filter):
    """Handler-level variant of the same redaction (the record factory below
    is what the application installs; this is for attaching one by hand)."""

    def __init__(self, secrets=()):
        super().__init__()
        self._secrets = list(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record, self._secrets)
        return True


def quiet_http_client_logs() -> None:
    """Pin credential-bearing third-party HTTP loggers (and every existing
    child, e.g. ccxt.base.exchange) to WARNING. Idempotent."""
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in list(logging.root.manager.loggerDict):
        if any(name.startswith(prefix + ".") for prefix in _THIRD_PARTY_LOGGERS):
            logging.getLogger(name).setLevel(logging.WARNING)


def install_log_safety(settings=None) -> None:
    """Idempotent. Pins third-party client loggers and redacts every log
    record from now on. Call at process start (main.py, API lifespan)."""
    global _base_factory
    quiet_http_client_logs()
    if settings is not None:
        _secrets.update(secrets_from_settings(settings))
    current = logging.getLogRecordFactory()
    if getattr(current, "_mba_redacting", False):
        return
    _base_factory = current

    def factory(*args, **kwargs):
        record = current(*args, **kwargs)
        _redact_record(record)
        return record

    factory._mba_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def uninstall_log_safety() -> None:
    """Undo install_log_safety's record factory and forget the secrets (tests)."""
    global _base_factory
    if _base_factory is not None:
        logging.setLogRecordFactory(_base_factory)
        _base_factory = None
    _secrets.clear()
