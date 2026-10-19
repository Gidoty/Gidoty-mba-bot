"""Deterministic, auditable pair ranking for Auto Select mode.

This is explicitly NOT an AI/ML model and NOT a "biggest gainer" screen -
every candidate is scored with the platform's own existing EMA/ATR
strategy (`strategy.generate_signal`), the exact same indicators and
crossover/flat-market rules already used for a manually-chosen pair. Auto
Select is provably "the same strategy, applied across a small fixed
universe" rather than a separate, unaudited algorithm bolted on top. Same
inputs always produce the same ranking - there is nothing probabilistic or
opaque to explain to a customer or support ticket.
"""
from __future__ import annotations

from dataclasses import dataclass

from .strategy import Candle, SignalType, generate_signal


@dataclass(frozen=True)
class PairCandidate:
    pair: str
    candles: list[Candle]


@dataclass(frozen=True)
class RankedPair:
    pair: str
    signal: SignalType
    score: float


def rank_candidates(
    candidates: list[PairCandidate],
    *,
    market_type: str,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
    atr_flat_threshold: float,
    atr_rolling_window: int,
) -> list[RankedPair]:
    """Scores every candidate with `strategy.generate_signal` and keeps
    only the ones with an actionable signal for `market_type` - long-only
    on spot, long or short on futures, the exact same rule trader.py's
    `_maybe_enter` already enforces, so Auto Select can never produce a
    side the account's market type wouldn't otherwise be allowed to take.

    `score` is the EMA separation measured in units of the pair's own
    ATR (`abs(fast_ema - slow_ema) / atr`) - how decisive this crossover
    is relative to that pair's own typical volatility, not its raw price
    move. A pair whose EMAs just barely crossed scores low even if its
    absolute price swing was large; a pair with a wide EMA gap relative
    to a tight ATR scores high. Returned sorted highest-score first;
    callers pick index 0 for "the best currently-ranked candidate", or
    none if the list is empty (no candidate had an actionable signal this
    cycle - Auto Select has nothing to switch to, and must not force one)."""
    ranked: list[RankedPair] = []
    for candidate in candidates:
        signal = generate_signal(
            candidate.candles,
            ema_fast_period=ema_fast_period,
            ema_slow_period=ema_slow_period,
            atr_period=atr_period,
            atr_flat_threshold=atr_flat_threshold,
            atr_rolling_window=atr_rolling_window,
        )
        if signal.signal == SignalType.NONE:
            continue
        if signal.signal == SignalType.BEARISH_CROSSOVER and market_type != "futures":
            continue
        if not signal.atr:
            continue
        score = abs(signal.fast_ema - signal.slow_ema) / signal.atr
        ranked.append(RankedPair(pair=candidate.pair, signal=signal.signal, score=score))
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked
