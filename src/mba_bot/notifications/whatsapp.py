from __future__ import annotations

from .base import Notifier


class WhatsAppNotifier(Notifier):
    """Placeholder. The product spec ships Telegram first and adds
    WhatsApp later; this stub exists only so `notification_channel` can
    already accept `'whatsapp'` at signup and the eventual implementation
    (WhatsApp Cloud API or a provider like Twilio) is a drop-in here
    without touching trader.py or the notifier interface."""

    def __init__(self, provider_token: str | None):
        self._provider_token = provider_token

    async def send(self, target: str, message: str) -> None:
        raise NotImplementedError("WhatsApp notifications are not implemented yet - use 'telegram' for now")
