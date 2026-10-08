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

    if not text.startswith("/start ") or chat_id is None:
        return {"ok": True}

    token = text.removeprefix("/start ").strip()
    account = get_account_by_telegram_link_token(db, token)
    if account is None:
        logger.info("telegram webhook: no account matches link token")
        return {"ok": True}

    account.notification_target = str(chat_id)
    account.telegram_link_token = None

    if settings.telegram_bot_token:
        try:
            await TelegramNotifier(settings.telegram_bot_token).send(
                str(chat_id),
                "Your MBA Bot account is linked. Trade entries, exits, and daily P&L summaries will be sent here.",
            )
        except Exception:
            logger.exception("telegram webhook: failed to send link-confirmation message")

    return {"ok": True}
