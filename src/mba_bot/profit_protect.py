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


# ---------------------------------------------------------------------------------------------------- Part C: the staged profit lock
@dataclass(frozen=True)
class LockRules:
    """The staged lock's parameters, every one OFF at 0 (docs/safety/profit_staged_lock.md). R is the distance from the entry to the initial stop."""

    stage1_r: float = 0.0
    stage1_atr: float = 0.0
    stage2_r: float = 0.0
    stage2_lock_r: float = 1.0
    trail_start_r: float = 0.0
    trail_atr: float = 2.0
    trail_tiers: tuple = ()  # ((from_r, atr_multiple), ...) ascending by from_r
    tighten_after_candles: int = 0
    tighten_trail_atr: float = 0.0

    @property
    def stage2_on(self) -> bool:
        """Stage 2 is on only if its lock is below its activation: a lock at or above the level that earns it would sit beyond the best price."""
        return self.stage2_r > 0 and 0 < self.stage2_lock_r < self.stage2_r

    @property
    def any_on(self) -> bool:
        return self.stage1_r > 0 or self.stage2_on or self.trail_start_r > 0


def lock_rules_from_settings(settings) -> LockRules:
    return LockRules(
        stage1_r=settings.profit_lock_stage1_r, stage1_atr=settings.profit_lock_stage1_atr, stage2_r=settings.profit_lock_stage2_r, stage2_lock_r=settings.profit_lock_stage2_lock_r,
        trail_start_r=settings.profit_trail_start_r, trail_atr=settings.profit_trail_atr, trail_tiers=tuple(settings.profit_trail_tiers),
        tighten_after_candles=settings.profit_tighten_after_candles, tighten_trail_atr=settings.profit_tighten_trail_atr,
    )


def cost_buffer(entry: float, *, fee_bps_per_side: float, slippage_bps_round_trip: float, funding_paid_per_unit: float = 0.0) -> float:
    """The price distance a stop must sit beyond the entry to be at or above zero NET: both fees at `fee_bps_per_side`, the assumed round-trip slippage, and the funding paid so far per unit (a funding amount
    RECEIVED, a negative number, is ignored: it never shrinks the buffer). Always at least 0."""
    return max(0.0, entry * (2.0 * fee_bps_per_side + slippage_bps_round_trip) / 10_000.0) + max(0.0, funding_paid_per_unit)


def trail_multiplier(rules: LockRules, gain_r: float, held_candles: float) -> float:
    """The trail distance in ATRs at this profit and holding time: the tier whose from_r the profit has reached (the tightest tier reached, tiers sorted ascending), else the plain `trail_atr`; once the trade has
    been held tighten_after_candles candles it is also capped at tighten_trail_atr. Never below 0.5 ATR."""
    mult = rules.trail_atr
    for from_r, m in rules.trail_tiers:
        if gain_r >= from_r:
            mult = m
    if rules.tighten_after_candles > 0 and rules.tighten_trail_atr > 0 and held_candles >= rules.tighten_after_candles:
        mult = min(mult, rules.tighten_trail_atr)
    return max(0.5, mult)


def staged_level(*, side: str, entry: float, initial_stop: float, peak: float | None, atr: float | None, held_candles: float, rules: LockRules, buffer: float) -> tuple[float, str] | None:
    """The best stop level the staged lock has earned from the running best price `peak`, with its close reason, or None. Each stage is judged on the PEAK (the best the trade has done), not on where the price is now.
      stage 1 -> "break_even_stop"   entry +/- buffer, or the better of that and peak -/+ stage1_atr x ATR
      stage 2 -> "profit_lock"       entry +/- stage2_lock_r x R
      trail   -> "trailing_stop"     peak -/+ multiplier x ATR
    The most favourable earned level wins. A level that is not strictly on the loss side of the peak is dropped: a stop can only lock a level the trade has already gone beyond (when R is smaller than the cost buffer,
    stage 1 simply waits until the gain exceeds the buffer). This function never looks at the stop resting: profit_protect.decide never loosens it."""
    r = abs(entry - initial_stop)
    if peak is None or not r > 0 or not rules.any_on:
        return None
    long_side = side == "long"
    gain = (peak - entry) if long_side else (entry - peak)
    gain_r = gain / r
    cands: list[tuple[float, str]] = []
    if rules.stage1_r > 0 and gain_r >= rules.stage1_r:
        lvl = entry + buffer if long_side else entry - buffer
        if rules.stage1_atr > 0 and atr is not None and atr > 0:
            wide = peak - rules.stage1_atr * atr if long_side else peak + rules.stage1_atr * atr
            lvl = max(lvl, wide) if long_side else min(lvl, wide)
        cands.append((lvl, "break_even_stop"))
    if rules.stage2_on and gain_r >= rules.stage2_r:
        cands.append((entry + rules.stage2_lock_r * r if long_side else entry - rules.stage2_lock_r * r, "profit_lock"))
    if rules.trail_start_r > 0 and gain_r >= rules.trail_start_r and atr is not None and atr > 0:
        m = trail_multiplier(rules, gain_r, held_candles)
        cands.append((peak - m * atr if long_side else peak + m * atr, "trailing_stop"))
    cands = [c for c in cands if is_better(side, peak, c[0])]  # on the loss side of the best price
    if not cands:
        return None
    return max(cands, key=lambda c: c[0]) if long_side else min(cands, key=lambda c: c[0])
