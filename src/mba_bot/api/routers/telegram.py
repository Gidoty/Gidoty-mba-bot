"""Telegram account linking.

A customer can't type in their own numeric chat id, so instead: we hand
them a deep link carrying a one-time token
(`https://t.me/<bot>?start=<token>`); tapping it sends our bot a `/start
<token>` message; Telegram relays that to `/api/telegram/webhook`, which
resolves the token back to the account and fills in
`notification_target` with the resulting chat id. `trader.py` refuses to
trade an account until `notification_target` is set, so this is a real
onboarding gate, not just a nice-to-have.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ...config import Settings
from ...db import get_account_by_telegram_link_token
from ...models import Customer
from ...notifications.telegram import TelegramNotifier
from ..deps import get_current_customer, get_db, get_settings
from ..schemas import TelegramLinkOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.get("/link-url", response_model=TelegramLinkOut)
def get_link_url(
    customer: Customer = Depends(get_current_customer), settings: Settings = Depends(get_settings)
) -> TelegramLinkOut:
    account = customer.account
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet")
    if not settings.telegram_bot_username:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TELEGRAM_BOT_USERNAME is not configured")

    if not account.telegram_link_token:
        account.telegram_link_token = secrets.token_urlsafe(24)

    return TelegramLinkOut(link_url=f"https://t.me/{settings.telegram_bot_username}?start={account.telegram_link_token}")


async def _reply(chat_id: str, message: str, *, settings: Settings) -> None:
    """Best-effort send - a failure here must never turn into a non-200
    response to Telegram, or it'll just retry the same update forever."""
    if not settings.telegram_bot_token:
        return
    try:
        await TelegramNotifier(settings.telegram_bot_token).send(chat_id, message)
    except Exception:
        logger.exception("telegram webhook: failed to send a reply to chat_id=%s", chat_id)


_WELCOME_MESSAGE = (
    "Welcome to MBA Bot. To receive trade alerts here, open your dashboard, go to the Telegram "
    "step, and tap the \"Open Telegram and link\" link from there - it carries a one-time code "
    "that connects this chat to your account. This plain /start doesn't link anything by itself."
)

_INVALID_TOKEN_MESSAGE = (
    "That link has expired or was already used. Open your dashboard and tap \"Open Telegram and "
    "link\" again to get a fresh one."
)

_LINKED_MESSAGE = "Your MBA Bot account is linked. Trade entries, exits, and daily P&L summaries will be sent here."


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict:
    # Telegram just needs a 200 response; errors here are logged and
    # swallowed rather than raised, since a non-200 makes Telegram retry
    # the same update indefinitely.
    try:
        payload = await request.json()
    except ValueError:
        return {"ok": True}

    message = payload.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if chat_id is None or not (text == "/start" or text.startswith("/start ")):
        return {"ok": True}

    token = text[len("/start") :].strip()
    if not token:
        # A bare /start - either someone poking the bot directly, or a
        # deep link opened without its ?start= parameter surviving.
        await _reply(str(chat_id), _WELCOME_MESSAGE, settings=settings)
        return {"ok": True}

    account = get_account_by_telegram_link_token(db, token)
    if account is None:
        logger.info("telegram webhook: no account matches link token")
        await _reply(str(chat_id), _INVALID_TOKEN_MESSAGE, settings=settings)
        return {"ok": True}

    account.notification_target = str(chat_id)
    account.telegram_link_token = None
    await _reply(str(chat_id), _LINKED_MESSAGE, settings=settings)

    return {"ok": True}
