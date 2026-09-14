from __future__ import annotations

import httpx

from .base import Notifier


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str):
        self._api_base = f"https://api.telegram.org/bot{bot_token}"

    async def send(self, target: str, message: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{self._api_base}/sendMessage", json={"chat_id": target, "text": message})
            response.raise_for_status()
