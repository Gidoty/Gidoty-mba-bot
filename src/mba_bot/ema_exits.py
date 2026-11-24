"""Exit rules for EMA trades: take profit, trailing stop and break-even, all measured in R (the distance from the entry to the INITIAL stop). Pure: no I/O, no clock, no database.
Docs: docs/safety/ema_exit_rules.md. Every rule is OFF at the default (value 0), and with all three off nothing here changes an EMA trade.

The rules, for a long (a short is the mirror image), over the CLOSED candles that started after the entry:
  * the stop starts at the initial stop;
  * take profit: the candle's high reaching entry + take_profit_r x R exits at that price;
  * trailing: once the best high since entry is at least trail_trigger_r x R above the entry, the stop is the best high minus trail_atr x ATR of that candle, ratcheted (never lowered);
  * break-even: once the best high is at least breakeven_r x R above the entry, the stop is at least the entry price.
A stop level earned by a candle applies from the NEXT candle (the order of events inside a candle is unknown). When one candle touches both the stop and the take profit, the STOP wins (the conservative reading, as in the
other strategies' replays). A candle that opens beyond a level fills at its open, the worse price."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitRules:
    take_profit_r: float = 0.0
    trail_trigger_r: float = 0.0
    trail_atr: float = 2.0
    breakeven_r: float = 0.0

    @property
    def any_on(self) -> bool:
        return self.take_profit_r > 0 or self.trail_trigger_r > 0 or self.breakeven_r > 0


def rules_from_settings(settings) -> ExitRules:
    return ExitRules(take_profit_r=settings.ema_take_profit_r, trail_trigger_r=settings.ema_trail_trigger_r, trail_atr=settings.ema_trail_atr, breakeven_r=settings.ema_breakeven_r)


def r_distance(entry: float, initial_stop: float) -> float | None:
    d = abs(entry - initial_stop)
    return d if d > 0 else None


def take_profit_level(side: str, entry: float, initial_stop: float, take_profit_r: float) -> float | None:
    """The price at which take profit fires, or None when the rule is off or R is unusable. Always on the PROFIT side of the entry."""
    r = r_distance(entry, initial_stop)
    if take_profit_r <= 0 or r is None:
        return None
    return entry + take_profit_r * r if side == "long" else entry - take_profit_r * r


def take_profit_touched(side: str, level: float | None, price: float) -> bool:
    """True when `price` has reached the take-profit level: at or above it for a long, at or below it for a short. Never true on the loss side."""
    if level is None:
        return False
    return price >= level if side == "long" else price <= level


@dataclass(frozen=True)
class Walk:
    """What the rules say about the candles after the entry. `stop` is the protective level EARNED so far (never worse than the initial stop). `exit_reason` is stop_loss, trailing_stop, break_even_stop or
    take_profit if a candle ended the trade, with its `exit_price` and candle `exit_ts`; None while the trade is still running."""

    stop: float
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_ts: int | None = None


def walk(side: str, entry: float, initial_stop: float, opened_ms: int, closed: list, rules: ExitRules, atr_values: list | None) -> Walk:
    """Walk the closed candles that started at or after the entry (the candle the entry happened in is skipped: the part before the entry never was ours). Deterministic: the result depends only on the
    candles. `atr_values[i]` is the ATR at `closed[i]` (None allowed)."""
    stop = initial_stop
    r = r_distance(entry, initial_stop)
    tp = take_profit_level(side, entry, initial_stop, rules.take_profit_r)
    long_side = side == "long"
    extreme = entry
    reason_of_stop = "stop_loss"
    for i, c in enumerate(closed):
        if c.timestamp < opened_ms:
            continue
        # 1. the stop that stood BEFORE this candle (the stop wins when both are touched)
        if long_side and c.low <= stop:
            return Walk(stop, reason_of_stop, min(stop, c.open), c.timestamp)
        if (not long_side) and c.high >= stop:
            return Walk(stop, reason_of_stop, max(stop, c.open), c.timestamp)
        # 2. take profit
        if tp is not None and ((long_side and c.high >= tp) or ((not long_side) and c.low <= tp)):
            return Walk(stop, "take_profit", max(tp, c.open) if long_side else min(tp, c.open), c.timestamp)
        # 3. what this candle earns, applying from the next candle
        if r is None:
            continue
        extreme = max(extreme, c.high) if long_side else min(extreme, c.low)
        gain = (extreme - entry) if long_side else (entry - extreme)
        if rules.breakeven_r > 0 and gain >= rules.breakeven_r * r:
            level = entry
            if (long_side and level > stop) or ((not long_side) and level < stop):
                stop, reason_of_stop = level, "break_even_stop"
        if rules.trail_trigger_r > 0 and gain >= rules.trail_trigger_r * r and atr_values is not None and atr_values[i] is not None:
            cand = extreme - rules.trail_atr * atr_values[i] if long_side else extreme + rules.trail_atr * atr_values[i]
            if (long_side and cand > stop) or ((not long_side) and cand < stop):
                stop, reason_of_stop = cand, "trailing_stop"
    return Walk(stop)


def earned_stop(side: str, entry: float, initial_stop: float, opened_ms: int, closed: list, rules: ExitRules, atr_values: list | None) -> float | None:
    """The protective stop the closed candles have earned (trailing and/or break-even), or None when it is no better than the initial stop or no rule that moves a stop is on. The caller moves the live stop to it
    only through `lifecycle.move_stop`, which refuses anything that is not strictly better than the stop resting and anything on the wrong side of the price."""
    if rules.trail_trigger_r <= 0 and rules.breakeven_r <= 0:
        return None
    w = walk(side, entry, initial_stop, opened_ms, closed, ExitRules(0.0, rules.trail_trigger_r, rules.trail_atr, rules.breakeven_r), atr_values)
    if w.exit_reason is not None:
        return None  # the walk already ended in an exit: the stop that stood is the exit's business
    better = w.stop > initial_stop if side == "long" else w.stop < initial_stop
    return w.stop if better else None
