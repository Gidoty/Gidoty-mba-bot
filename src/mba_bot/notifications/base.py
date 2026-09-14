from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    @abstractmethod
    async def send(self, target: str, message: str) -> None:
        """`target` is channel-specific: a Telegram chat id, a WhatsApp
        phone number, etc - whatever `TradingAccount.notification_target`
        holds for that account's channel."""
