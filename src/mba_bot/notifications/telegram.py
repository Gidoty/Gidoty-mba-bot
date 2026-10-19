from __future__ import annotations

import httpx

from .. import log_safety
from .base import Notifier

# Telegram's Bot API carries the bot token IN the request URL, and httpx logs
# request URLs at INFO: pin library request logging to WARNING for any
# process that can send a Telegram message (see log_safety).
log_safety.quiet_http_client_logs()


class NotificationError(Exception):
    """A failed notification, carrying ONLY what is safe for ordinary logs.

    Deliberately holds just a category and (when there is one) an HTTP status
    code - never the URL (which contains the Telegram bot token), headers,
    request body, response body or the original exception. It is raised from
    outside any `except` block, so it has no `__cause__` and no `__context__`
    for a traceback printer to walk back to the token-bearing httpx error.
    """

    def __init__(self, category: str, status_code: int | None = None):
        self.category = category  # "http" | "timeout" | "network" | "unexpected"
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code is not None else category
        super().__init__(f"Telegram send failed: {detail}")

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429

    @property
    def is_blocked_or_unauthorized(self) -> bool:
        return self.status_code in (401, 403)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str):
        self._api_base = f"https://api.telegram.org/bot{bot_token}"

    def __repr__(self) -> str:  # the default repr would be fine, but never let the token be one edit away
        return "TelegramNotifier(<redacted>)"

    async def send(self, target: str, message: str) -> None:
        failure: tuple[str, int | None] | None = None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(f"{self._api_base}/sendMessage", json={"chat_id": target, "text": message})
            if response.status_code >= 400:
                failure = ("http", response.status_code)
        except httpx.TimeoutException:
            failure = ("timeout", None)
        except httpx.HTTPError:
            failure = ("network", None)
        except Exception:
            failure = ("unexpected", None)
        # Raised here, outside the handlers above: no implicit exception
        # context, no chain, and nothing but the two values below survives.
        if failure is not None:
            raise NotificationError(*failure)
