"""Customer leverage setting and the size-cap mode (docs/safety/customer_leverage.md).

`trading_accounts.max_leverage` is the most position value (as a multiple of the account's capital) a customer lets the bot open on Futures. It is a whole number
from 1 to 5, read as 3 when empty, and never above the global hard limit `FUTURES_MAX_LEVERAGE` when that is set. Binance keeps its own leverage and margin mode per symbol;
this module never reads or writes them (the trader reads them) and the bot never sets either on Binance.

Pure functions only: the same code path for every account."""
from __future__ import annotations

import math

LEVERAGE_MIN = 1
LEVERAGE_MAX = 5
LEVERAGE_DEFAULT = 3

MODE_STRICT = "strict"
MODE_SIZE_CAP = "size_cap"


def parse_mode(raw: str | None) -> str:
    """strict | size_cap. Unset, empty or anything unrecognised is strict (today's behaviour), never size_cap."""
    return MODE_SIZE_CAP if (raw or "").strip().lower() == MODE_SIZE_CAP else MODE_STRICT


def limit(global_ceiling: float | None) -> int:
    """The highest number a customer may choose: 5, or the global hard limit (whole part) when that is set and lower. Never below 1."""
    if isinstance(global_ceiling, bool) or not isinstance(global_ceiling, (int, float)) or not math.isfinite(global_ceiling) or global_ceiling <= 0:
        return LEVERAGE_MAX
    return max(LEVERAGE_MIN, min(LEVERAGE_MAX, int(math.floor(global_ceiling))))


def stored_or_default(value) -> int:
    """The stored number if it is a whole number from 1 to 5, otherwise the default 3 (empty, bad data, out of range)."""
    if isinstance(value, bool) or not isinstance(value, int) or not LEVERAGE_MIN <= value <= LEVERAGE_MAX:
        return LEVERAGE_DEFAULT
    return value


def account_cap(stored, global_ceiling: float | None) -> int:
    """The number the bot uses for an account: the stored value (3 if empty), never above the global limit."""
    return min(stored_or_default(stored), limit(global_ceiling))


def validate(value, global_ceiling: float | None) -> int:
    """A customer's new number. Raises ValueError with a plain message unless it is a whole number from 1 to the allowed limit."""
    top = limit(global_ceiling)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Leverage must be a whole number from {LEVERAGE_MIN} to {top}.")
    if not LEVERAGE_MIN <= value <= top:
        raise ValueError(f"Leverage must be a whole number from {LEVERAGE_MIN} to {top}.")
    return value


def effective(binance_leverage: float, cap: int) -> float:
    """The multiple the position value is capped at: the smaller of Binance's number for the pair and the customer's number."""
    return min(float(binance_leverage), float(cap))


# Customer-facing refusal text (docs/customer/leverage_help.md has the reviewed wording). Fixed strings: nothing from the exchange is ever put in them.
MSG_NOT_ISOLATED = (
    "your Binance Futures margin mode on {pair} is not isolated (or could not be read), and this service trades isolated margin only. "
    "On Binance, open the Futures page for {pair}, find the Margin Mode control and choose Isolated, then try again. MBA Bot never changes it for you."
)
