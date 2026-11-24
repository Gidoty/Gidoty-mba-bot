"""Profit protection shared by every strategy (docs/safety/profit_peak_tracking.md and the parts that follow). Pure: no I/O, no clock, no database.

Part A (this file so far): the best price reached since the entry, also called the peak or the maximum favourable excursion. For a long it is the highest price seen since the entry, for a short the lowest. It only
ever moves in the favourable direction. Nothing here is read when deciding an entry."""
from __future__ import annotations


def is_better(side: str, a: float, b: float) -> bool:
    """True when price `a` is strictly more favourable than `b` for this side: higher for a long, lower for a short."""
    return a > b if side == "long" else a < b


def advance_peak(side: str, previous: float | None, candidate: float | None) -> float | None:
    """The peak after seeing `candidate`: never worse than `previous`. A missing or non-positive candidate changes nothing."""
    if candidate is None or not candidate > 0:
        return previous
    if previous is None:
        return candidate
    return candidate if is_better(side, candidate, previous) else previous


def peak_from_candles(side: str, entry: float, opened_ms: int, candles: list, price: float | None, previous: float | None) -> float:
    """The peak so far: the stored `previous` (or the entry price when none is stored), then the highest high (long) or lowest low (short) of every candle that STARTED at or after the entry, then the live `price`.
    A candle that started before the entry is skipped: its extreme may have happened before the trade existed. The result is never worse than the entry price."""
    best = advance_peak(side, previous, entry) if previous is not None else entry
    for c in candles:
        if c.timestamp < opened_ms:
            continue
        best = advance_peak(side, best, c.high if side == "long" else c.low)
    return advance_peak(side, best, price)


# ---------------------------------------------------------------------------------------------------- Part B: one decision per round
from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class Decision:
    """What to do with a protective-stop level worked out this round: `exit` (the price is already through the level, so the stop would trigger at once: leave through execute_exit), `move` (place the new stop
    through move_stop) or `hold` (do nothing, with the reason)."""

    action: str
    why: str


def decide(*, side: str, level: float | None, current_stop: float, price: float, entry: float, atr: float | None, r: float | None,
           last_move_ms: int | None, now_ms: int, min_interval_s: float, min_improve_atr: float) -> Decision:
    """The single decision for a stop level worked out from the running best price.
    * No level, or a level that is not strictly better than the stop resting: hold. A stop is never loosened and never re-placed at the same level.
    * The price is at or through the level (at or below it for a long): the lock was earned and is already lost, so exit. This is never delayed by the call limits below.
    * Otherwise the move must be worth its exchange calls: at least `min_improve_atr` x ATR better than the stop resting (0.1 R when no ATR is known, nothing when neither is known), and at least `min_interval_s`
      since the last move. The first move that takes the stop off its loss side of the entry (the stop still worse than the entry) is exempt from the interval, not from the improvement."""
    if level is None or not level > 0:
        return Decision("hold", "no_level")
    if not is_better(side, level, current_stop):
        return Decision("hold", "not_better")
    if (side == "long" and price <= level) or (side == "short" and price >= level):
        return Decision("exit", "price_through_level")
    gain = abs(level - current_stop)
    need = min_improve_atr * atr if atr and atr > 0 else (0.1 * r if r and r > 0 else 0.0)
    if gain < need:
        return Decision("hold", "improvement_too_small")
    unlocked_yet = is_better(side, entry, current_stop)  # the resting stop is still worse than the entry: the trade has no profit lock
    if not unlocked_yet and last_move_ms is not None and now_ms - last_move_ms < min_interval_s * 1000:
        return Decision("hold", "too_soon")
    return Decision("move", "ok")
