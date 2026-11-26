"""A small outbound email sender (docs/safety/email_and_recovery.md). Two transports behind one interface, chosen by EMAIL_TRANSPORT: `smtp` (a mail server the owner runs or rents) or `http` (a mail provider's HTTP API:
one POST with a bearer key and a JSON body {from, to, subject, text}; the exact shape is UNVERIFIED against any provider). `off` (the default) builds no sender at all.

Credentials come from the environment, entered by the owner; they are never logged, never put in the database and never returned by any route. Nothing here logs an address or a message body: a failure logs
the exception class only. `send` never raises: it returns True or False."""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Awaitable, Callable

from .config import Settings

logger = logging.getLogger(__name__)
_ADDRESS = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str


def clean_subject(subject: str) -> str:
    return " ".join(str(subject).split())[:150]


def valid_address(value: str) -> bool:
    return bool(isinstance(value, str) and _ADDRESS.match(value))


class SmtpMailer:
    def __init__(self, settings: Settings, factory: Callable | None = None):
        self._s = settings
        self._factory = factory

    def _send_blocking(self, msg: Message) -> None:
        s = self._s
        email = EmailMessage()
        email["From"], email["To"], email["Subject"] = s.email_from, msg.to, clean_subject(msg.subject)
        email.set_content(msg.text)
        if self._factory is not None:
            client = self._factory(s.email_smtp_host, s.email_smtp_port, timeout=TIMEOUT_SECONDS)
        elif s.email_smtp_security == "ssl":
            client = smtplib.SMTP_SSL(s.email_smtp_host, s.email_smtp_port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(s.email_smtp_host, s.email_smtp_port, timeout=TIMEOUT_SECONDS)
        with client:
            if s.email_smtp_security == "starttls":
                client.starttls(context=ssl.create_default_context())
            if s.email_smtp_user:
                client.login(s.email_smtp_user, s.email_smtp_password or "")
            client.send_message(email)

    async def send(self, msg: Message) -> bool:
        if not valid_address(msg.to):
            return False
        try:
            await asyncio.to_thread(self._send_blocking, msg)
            return True
        except Exception as exc:  # noqa: BLE001 - class name only: the message can hold an address
            logger.warning("an email could not be sent (smtp, %s)", type(exc).__name__)
            return False


HttpPost = Callable[[str, dict, dict], Awaitable[int]]


async def _httpx_post(url: str, headers: dict, body: dict) -> int:
    import httpx

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        return (await client.post(url, headers=headers, json=body)).status_code


class HttpMailer:
    def __init__(self, settings: Settings, post: HttpPost | None = None):
        self._s = settings
        self._post = post or _httpx_post

    async def send(self, msg: Message) -> bool:
        if not valid_address(msg.to):
            return False
        s = self._s
        try:
            code = await self._post(s.email_http_url, {"Authorization": f"Bearer {s.email_http_key}", "Content-Type": "application/json"},
                                    {"from": s.email_from, "to": [msg.to], "subject": clean_subject(msg.subject), "text": msg.text})
            if 200 <= code < 300:
                return True
            logger.warning("an email could not be sent (http status %s)", code)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("an email could not be sent (http, %s)", type(exc).__name__)
            return False


def configured(settings: Settings) -> bool:
    """True only when the transport is chosen AND every setting it needs is present."""
    if not valid_address(settings.email_from or ""):
        return False
    if settings.email_transport == "smtp":
        return bool(settings.email_smtp_host and (not settings.email_smtp_user or settings.email_smtp_password))
    if settings.email_transport == "http":
        return bool(settings.email_http_url.startswith("https://") and settings.email_http_key)
    return False


def build_mailer(settings: Settings, *, smtp_factory: Callable | None = None, http_post: HttpPost | None = None):
    """The sender for these settings, or None when email is off or half set up."""
    if not configured(settings):
        return None
    return SmtpMailer(settings, smtp_factory) if settings.email_transport == "smtp" else HttpMailer(settings, http_post)
