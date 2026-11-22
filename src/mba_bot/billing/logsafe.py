"""One-line, secret-free causes for billing log lines. A webhook or a Paystack call can fail with text that carries a key, a signature, a token or an e-mail address; none of that may reach the journal. `safe_cause` keeps
the exception class and the first line of its message with every long token, e-mail address and header value removed, cut to a short length."""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-./+=]{24,}")  # keys (sk_live_...), signatures (hex), tokens, long references
_HEADER = re.compile(r"(?i)(authorization|bearer|x-paystack-signature|api[_-]?key|secret|token|signature)\s*[:=]?\s*\S+")


def safe_cause(exc: BaseException, limit: int = 160) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    text = _EMAIL.sub("<email>", text)
    text = _HEADER.sub(lambda m: m.group(1) + " <removed>", text)
    text = _LONG_TOKEN.sub("<removed>", text)
    return text[:limit]
