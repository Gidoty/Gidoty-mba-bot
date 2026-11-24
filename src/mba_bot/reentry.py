"""Re-entry after a protective exit (Part J, docs/safety/reentry.md). Pure: no I/O, no clock, no database. OFF by default.

After an EMA trade left through a PROTECTIVE exit (break-even stop, trailing stop or profit lock), the trend can carry on without a new EMA crossover, and the crossover rule would never enter again. This module answers one
question: should the bot try ONE more entry on the same pair and side now? The entry itself is an ordinary entry made by the caller through the same guards, risk, stop, protection check and fee guard as any other.
All conditions must hold:
  1. the previous trade was an EMA trade that closed with one of PROTECTIVE_REASONS (never a plain stop-out, a time stop or a reversal exit: those say the trend failed);
  2. between `cooldown_candles` and `window_candles` closed candles have passed since it closed;
  3. fewer than `max_per_trend` re-entries have already followed this trend (a chain of re-entries is one trend; a new EMA crossover starts a new one);
  4. the trend is intact: the fast EMA is on the trade's side of the slow EMA on the last closed candle;
  5. the last closed candle closed beyond the previous exit price by at least `above_exit_atr` x ATR (for a long: a new high above the previous exit level; for a short the mirror)."""
from __future__ import annotations

from dataclasses import dataclass

PROTECTIVE_REASONS = ("break_even_stop", "trailing_stop", "profit_lock")


@dataclass(frozen=True)
class ReentryRules:
    enabled: bool = False
    cooldown_candles: int = 2
    window_candles: int = 48
    max_per_trend: int = 1
    above_exit_atr: float = 1.0


def rules_from_settings(settings) -> ReentryRules:
    return ReentryRules(enabled=settings.reentry_enabled, cooldown_candles=settings.reentry_cooldown_candles, window_candles=settings.reentry_window_candles,
                        max_per_trend=settings.reentry_max_per_trend, above_exit_atr=settings.reentry_above_exit_atr)


def decide(*, rules: ReentryRules, side: str, prev_reason: str | None, prev_exit_price: float | None, candles_since_exit: int, reentries_so_far: int, fast: float | None, slow: float | None,
           last_close: float | None, atr: float | None) -> tuple[bool, str]:
    """(allowed, why). `candles_since_exit` is the number of CLOSED candles since the previous trade closed. Never allows anything with the rules off."""
    if not rules.enabled:
        return False, "off"
    if prev_reason not in PROTECTIVE_REASONS:
        return False, "previous_exit_not_protective"
    if prev_exit_price is None or not prev_exit_price > 0:
        return False, "no_exit_price"
    if candles_since_exit < rules.cooldown_candles:
        return False, "cooldown"
    if candles_since_exit > rules.window_candles:
        return False, "window_over"
    if reentries_so_far >= rules.max_per_trend:
        return False, "limit_per_trend"
    if fast is None or slow is None or last_close is None or atr is None or not atr > 0:
        return False, "no_data"
    if not (fast > slow if side == "long" else fast < slow):
        return False, "trend_not_intact"
    margin = rules.above_exit_atr * atr
    if not (last_close > prev_exit_price + margin if side == "long" else last_close < prev_exit_price - margin):
        return False, "not_beyond_exit_level"
    return True, "ok"
