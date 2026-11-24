"""Which strategies customers may choose, what a new account starts with, and the admin override (docs/safety/strategy_availability.md). Pure functions over the settings and an account row; no I/O.

What this does NOT do: it never switches a strategy on for trading. A strategy trades for an account only when the account is opted in (`extra_strategies`, as before) AND the operator's live switch for it is on
(LIVE_RSI_MR, LIVE_DONCHIAN, unchanged). Availability only decides what a customer may pick, and what a new account is given. Changing availability never touches an account that is already set up, and an
account's open trades are managed by their own strategy whatever the availability says."""
from __future__ import annotations

from .multi_trade import EMA_STRATEGY_ID, NEW_STRATEGY_IDS, account_strategies

DONCHIAN, RSI = "donchian_breakout_v1", "rsi_mr_v1"
NAMES = {EMA_STRATEGY_ID: "EMA crossover", DONCHIAN: "Donchian breakout", RSI: "RSI mean reversion"}
CHOICES = {"ema_only": (), "ema_donchian": (DONCHIAN,), "ema_rsi": (RSI,), "ema_donchian_rsi": (DONCHIAN, RSI)}


def available(settings) -> tuple[str, ...]:
    """The strategies a customer may choose, in a fixed order. EMA is always available (it is what every account trades today)."""
    out = [EMA_STRATEGY_ID]
    if settings.strategy_donchian_available:
        out.append(DONCHIAN)
    if settings.strategy_rsi_available:
        out.append(RSI)
    return tuple(out)


def default_for_new(settings) -> str | None:
    """The value of `extra_strategies` a NEW account is created with: the configured default, filtered to what is available. None (the column's empty value) is EMA only, exactly as every account is today."""
    wanted = CHOICES.get(settings.new_account_strategies, ())
    ok = [s for s in wanted if s in available(settings)]
    return ",".join(ok) or None


def selection(account) -> tuple[str, ...]:
    """What the account is opted in to now: EMA plus its extra strategies."""
    return (EMA_STRATEGY_ID,) + tuple(s for s in NEW_STRATEGY_IDS if s in account_strategies(account))


class ChoiceError(ValueError):
    pass


def validate_customer_choice(settings, wanted) -> str | None:
    """The `extra_strategies` value for a customer's choice, or ChoiceError. Only available strategies; EMA is implied; duplicates and unknown ids are refused, never silently dropped."""
    if not isinstance(wanted, (list, tuple)) or len(wanted) > 3:
        raise ChoiceError("send a list of strategy ids")
    ids = []
    for w in wanted:
        if not isinstance(w, str) or w not in NAMES:
            raise ChoiceError("unknown strategy")
        if w in ids:
            raise ChoiceError("a strategy is listed twice")
        ids.append(w)
    for w in ids:
        if w not in available(settings):
            raise ChoiceError(f"{NAMES[w]} is not available")
    extra = [s for s in NEW_STRATEGY_IDS if s in ids]
    return ",".join(extra) or None


def admin_value(wanted) -> str | None:
    """The `extra_strategies` value for an admin override: any known strategies, regardless of availability (that is what an override is). Unknown ids are refused."""
    if not isinstance(wanted, (list, tuple)):
        raise ChoiceError("send a list of strategy ids")
    ids = []
    for w in wanted:
        if w == EMA_STRATEGY_ID:
            continue
        if w not in NEW_STRATEGY_IDS or w in ids:
            raise ChoiceError("unknown or repeated strategy")
        ids.append(w)
    return ",".join(s for s in NEW_STRATEGY_IDS if s in ids) or None
