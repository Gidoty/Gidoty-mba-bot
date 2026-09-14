from __future__ import annotations

from .base import Notifier
from .telegram import TelegramNotifier
from .whatsapp import WhatsAppNotifier

__all__ = ["Notifier", "TelegramNotifier", "WhatsAppNotifier", "build_notifier"]


def build_notifier(channel: str, *, telegram_bot_token: str | None, whatsapp_provider_token: str | None) -> Notifier:
    if channel == "telegram":
        if not telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        return TelegramNotifier(telegram_bot_token)
    if channel == "whatsapp":
        return WhatsAppNotifier(whatsapp_provider_token)
    raise ValueError(f"unknown notification channel: {channel!r}")
