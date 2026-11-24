"""The +1R profit lock (docs/safety/profit_lock.md). Pure: no I/O, no clock, no database. OFF by default (PROFIT_LOCK_R=0).

Once a trade's best price has been at least `lock_r` x R beyond the entry (R = the distance from the entry to the INITIAL stop), its protective stop is moved to the entry plus the round-trip cost buffer (both fees, the
assumed slippage, the funding paid so far), so a stop-out is at or above zero net instead of at the original loss. It is the same rule for EMA, Donchian and RSI trades. This module only decides; the caller moves a stop
through lifecycle.move_stop (the new stop is placed and read back before the old one is cancelled, favourable direction only, never on the wrong side of the price) and exits through lifecycle.execute_exit."""
from __future__ import annotations

from .profit_protect import is_better


def lock_level(*, side: str, entry: float, initial_stop: float | None, peak: float | None, lock_r: float, buffer: float) -> float | None:
    """The stop level the lock asks for, or None. None while the rule is off, R cannot be measured, the best price has not been `lock_r` x R beyond the entry, or the level is not on the loss side of the best price
    (when R is smaller than the cost buffer the lock simply waits until the gain exceeds the buffer, instead of forcing an instant exit)."""
    if lock_r <= 0 or initial_stop is None or peak is None or not entry > 0:
        return None
    r = abs(entry - initial_stop)
    if not r > 0:
        return None
    long_side = side == "long"
    gain = (peak - entry) if long_side else (entry - peak)
    if gain < lock_r * r:
        return None
    level = entry + max(0.0, buffer) if long_side else entry - max(0.0, buffer)
    return level if is_better(side, peak, level) else None


def decide(*, side: str, level: float | None, current_stop: float, price: float, entry: float, atr: float | None, r: float | None, every_round: bool,
           last_move_ms: int | None, now_ms: int, min_interval_s: float, min_improve_atr: float) -> tuple[str, str]:
    """('hold' | 'move' | 'exit', why).
      * No level, or a level not strictly better than the stop that rests (a Donchian trail already above it, an earlier lock): hold. THE BETTER STOP WINS; a stop is never loosened or re-placed at the same level.
      * The price is at or through the level (at or below it for a long): the lock was earned and is already lost, so exit at the market (a stop placed there would trigger at once and is refused as wrong-side anyway).
      * Closed-candle mode: otherwise move.
      * Every-round mode: the move must also be worth its exchange calls: at least `min_improve_atr` x ATR better than the resting stop (0.1 R when no ATR is known), and `min_interval_s` since the last move.
        The first lock, while the resting stop is still worse than the entry, is never delayed by the interval (only later refinements are)."""
    if level is None or not level > 0:
        return "hold", "no_level"
    if not is_better(side, level, current_stop):
        return "hold", "not_better"
    if (side == "long" and price <= level) or (side == "short" and price >= level):
        return "exit", "price_through_level"
    if not every_round:
        return "move", "ok"
    need = min_improve_atr * atr if atr and atr > 0 else (0.1 * r if r and r > 0 else 0.0)
    if abs(level - current_stop) < need:
        return "hold", "improvement_too_small"
    first_lock = is_better(side, entry, current_stop)
    if not first_lock and last_move_ms is not None and now_ms - last_move_ms < min_interval_s * 1000:
        return "hold", "too_soon"
    return "move", "ok"
