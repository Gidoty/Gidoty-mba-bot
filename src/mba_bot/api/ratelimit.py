"""In-process rate limiting and login throttling (F-04). Used only when RATE_LIMIT_ENABLED / LOGIN_THROTTLE_ENABLED
are on; the Nginx limits (docs/security/nginx_rate_limit.md) are the first line and this is the second.

State lives in this process's memory. Production runs ONE worker (F-03), so one table is enough; a restart clears it.
Memory is bounded: old entries are dropped as they expire and the tables have a hard size cap. The client address is
`request.client.host`, which uvicorn replaces with the real address from X-Forwarded-For only when the request came
from the trusted local proxy; this module never reads the header itself, so a caller cannot choose their own key."""
from __future__ import annotations

import time
from collections import deque

from starlette.responses import JSONResponse

# Never limited: the providers' callbacks and the health probe.
EXEMPT_PATHS = frozenset({"/api/health", "/api/billing/webhook", "/api/telegram/webhook"})
AUTH_PATHS = frozenset({"/api/auth/login", "/api/auth/signup"})
_MAX_KEYS = 20_000


class SlidingWindowLimiter:
    def __init__(self, clock=time.monotonic):
        self._hits: dict[str, deque[float]] = {}
        self._clock = clock

    def hit(self, key: str, limit: int, window_seconds: float = 60.0) -> tuple[bool, int]:
        """Record one request. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False, max(1, int(window_seconds - (now - q[0])) + 1)
        q.append(now)
        if len(self._hits) > _MAX_KEYS:
            self._prune(now, window_seconds)
        return True, 0

    def _prune(self, now: float, window_seconds: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= window_seconds]:
            del self._hits[k]
        if len(self._hits) > _MAX_KEYS:  # still too many live keys: drop the oldest half rather than grow
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[: len(self._hits) // 2]:
                del self._hits[k]


class RateLimitMiddleware:
    """Pure ASGI, so it adds no per-request cost beyond a dictionary lookup."""

    def __init__(self, app, *, per_minute: int, auth_per_minute: int, limiter: SlidingWindowLimiter | None = None):
        self.app, self.per_minute, self.auth_per_minute = app, per_minute, auth_per_minute
        self.limiter = limiter or SlidingWindowLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        client = (scope.get("client") or ("unknown", 0))[0]
        checks = [(f"all:{client}", self.per_minute)]
        if scope["path"] in AUTH_PATHS and scope["method"] == "POST":
            checks.append((f"auth:{client}", self.auth_per_minute))
        for key, limit in checks:
            allowed, retry = self.limiter.hit(key, limit)
            if not allowed:
                response = JSONResponse({"detail": "too many requests, try again shortly"}, status_code=429, headers={"Retry-After": str(retry)})
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


class LoginThrottle:
    """Counts FAILED logins. Two keys, both including the client address, so one attacker cannot lock a victim out
    for everyone else: (address, email) and address overall. A success clears the (address, email) count."""

    def __init__(self, *, max_failures: int, lockout_seconds: int, clock=time.monotonic):
        self.max_failures, self.lockout, self._clock = max_failures, lockout_seconds, clock
        self._fails: dict[str, deque[float]] = {}

    def _count(self, key: str) -> int:
        q = self._fails.get(key)
        if not q:
            return 0
        now = self._clock()
        while q and now - q[0] >= self.lockout:
            q.popleft()
        return len(q)

    def blocked(self, ip: str, email: str) -> int:
        """Seconds to wait, or 0 if the attempt may proceed."""
        for key, limit in ((f"pair:{ip}|{email.lower()}", self.max_failures), (f"ip:{ip}", self.max_failures * 4)):
            if self._count(key) >= limit:
                return max(1, int(self.lockout - (self._clock() - self._fails[key][0])) + 1)
        return 0

    def record_failure(self, ip: str, email: str) -> None:
        now = self._clock()
        for key in (f"pair:{ip}|{email.lower()}", f"ip:{ip}"):
            self._fails.setdefault(key, deque()).append(now)
        if len(self._fails) > _MAX_KEYS:
            for k in [k for k in self._fails if self._count(k) == 0]:
                self._fails.pop(k, None)

    def record_success(self, ip: str, email: str) -> None:
        self._fails.pop(f"pair:{ip}|{email.lower()}", None)
