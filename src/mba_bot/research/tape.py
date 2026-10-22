"""Signal tapes: the production signal, computed once per poll and stored compactly.

The signal at a poll depends only on market data (the closed hourly candles plus the still-forming
one), never on the account's state. So the expensive part of a replay, running the production
`generate_signal` on a 200-candle window at every 5-minute poll, can be done once per pair and shared by
every experiment. A tape holds exactly what `generate_signal` returned for each poll (signal code, fast and
slow EMA, ATR, ATR%), produced by calling the production function itself; nothing is re-implemented.

Also here: hourly auxiliary series used only by research policies (a causal volatility percentile and
trailing returns). They look backwards only.
"""
from __future__ import annotations

import bisect
from array import array
from dataclasses import dataclass

from ..indicators import atr
from ..strategy import Candle, SignalType, StrategySignal, generate_signal

HOUR = 3_600_000
STEP_5M = 300_000

CODE_NA = -1  # not enough history yet
CODE_NONE = 0
CODE_BULL = 1
CODE_BEAR = 2
CODE_FLAT = 3  # a crossover that the flat-market filter rejected

_BY_CODE = {
    CODE_NONE: (SignalType.NONE, "no crossover"),
    CODE_BULL: (SignalType.BULLISH_CROSSOVER, "EMA fast crossed above slow"),
    CODE_BEAR: (SignalType.BEARISH_CROSSOVER, "EMA fast crossed below slow"),
    CODE_FLAT: (SignalType.NONE, "flat/range-bound market: ATR% below rolling threshold"),
}


def _code_of(sig: StrategySignal) -> int:
    if sig.signal == SignalType.BULLISH_CROSSOVER:
        return CODE_BULL
    if sig.signal == SignalType.BEARISH_CROSSOVER:
        return CODE_BEAR
    return CODE_FLAT if "flat" in (sig.reason or "") else CODE_NONE


@dataclass
class SignalTape:
    """One row per poll (replay) or per closed hour (closed)."""

    symbol: str
    kind: str  # "replay" | "closed"
    codes: array
    fast: array
    slow: array
    atr: array
    atr_pct: array

    def __len__(self) -> int:
        return len(self.codes)

    def code(self, i: int) -> int:
        return self.codes[i]

    def signal(self, i: int) -> StrategySignal | None:
        c = self.codes[i]
        if c == CODE_NA:
            return None
        sig, reason = _BY_CODE[c]
        nan = self.fast[i] != self.fast[i]
        return StrategySignal(sig, reason, None if nan else self.fast[i], None if nan else self.slow[i],
                              None if self.atr[i] != self.atr[i] else self.atr[i], None if self.atr_pct[i] != self.atr_pct[i] else self.atr_pct[i])


def _new(symbol: str, kind: str, n: int) -> SignalTape:
    nan = float("nan")
    return SignalTape(symbol, kind, array("b", [CODE_NA]) * n, array("d", [nan]) * n, array("d", [nan]) * n, array("d", [nan]) * n, array("d", [nan]) * n)


def _store(t: SignalTape, i: int, sig: StrategySignal) -> None:
    t.codes[i] = _code_of(sig)
    nan = float("nan")
    t.fast[i] = sig.fast_ema if sig.fast_ema is not None else nan
    t.slow[i] = sig.slow_ema if sig.slow_ema is not None else nan
    t.atr[i] = sig.atr if sig.atr is not None else nan
    t.atr_pct[i] = sig.atr_pct if sig.atr_pct is not None else nan


def build_closed_tape(symbol: str, hourly: list[Candle], sp: dict, *, limit: int, warm: int) -> SignalTape:
    """Signal on each CLOSED hour (window ends at that hour), indexed like `hourly`."""
    tape = _new(symbol, "closed", len(hourly))
    for i in range(warm, len(hourly)):
        _store(tape, i, generate_signal(hourly[max(0, i + 1 - limit) : i + 1], **sp))
    return tape


def build_replay_tape(symbol: str, hourly: list[Candle], timeline: list[int], exec_bars: list[Candle], sp: dict, *, limit: int, warm: int) -> SignalTape:
    """Signal at every poll of `timeline` (5-minute bar CLOSES), exactly as the service would see it: the
    closed hourly candles before the current hour plus the hour's still-forming candle."""
    by_ts = {c.timestamp: c for c in exec_bars}
    hour_pos = {c.timestamp: i for i, c in enumerate(hourly)}
    tape = _new(symbol, "replay", len(timeline))
    cur_hs = None
    o = h = lo = c = v = 0.0
    for k, ts in enumerate(timeline):
        b = by_ts[ts]
        hs = ts - ts % HOUR
        if hs != cur_hs:
            cur_hs, o, h, lo, c, v = hs, b.open, b.high, b.low, b.close, b.volume
        else:
            h = max(h, b.high)
            lo = min(lo, b.low)
            c = b.close
            v += b.volume
        idx = hour_pos.get(hs)
        if idx is None or idx < warm:
            continue
        window = hourly[max(0, idx - (limit - 1)) : idx] + [Candle(hs, o, h, lo, c, v)]
        _store(tape, k, generate_signal(window, **sp))
    return tape


# ------------------------------------------------------------------ hourly auxiliary series (causal)


@dataclass
class HourlyAux:
    """Aligned with the hourly candles. Value i uses hours <= i only. None = not enough history."""

    atr_pct: list[float | None]
    vol_pct: list[float | None]  # percentile rank of ATR% within the trailing window
    ret30d: list[float | None]
    ret72h: list[float | None]


VOL_WINDOW = 2160  # 90 days
VOL_MIN = 720  # need 30 days before a percentile means anything
RET30D = 720
RET72H = 72


def build_hourly_aux(hourly: list[Candle], atr_period: int = 14) -> HourlyAux:
    closes = [c.close for c in hourly]
    a = atr([c.high for c in hourly], [c.low for c in hourly], closes, atr_period)
    atr_pct = [(x / cl) if (x is not None and cl) else None for x, cl in zip(a, closes)]
    vol_pct: list[float | None] = [None] * len(hourly)
    window: list[float] = []  # sorted
    fifo: list[float] = []
    head = 0
    for i, v in enumerate(atr_pct):
        if v is None:
            continue
        bisect.insort(window, v)
        fifo.append(v)
        if len(fifo) - head > VOL_WINDOW:
            old = fifo[head]
            head += 1
            del window[bisect.bisect_left(window, old)]
        if len(window) >= VOL_MIN:
            vol_pct[i] = bisect.bisect_right(window, v) / len(window)
    ret30 = [None] * len(hourly)
    ret72 = [None] * len(hourly)
    for i in range(len(hourly)):
        if i >= RET30D and closes[i - RET30D] > 0:
            ret30[i] = closes[i] / closes[i - RET30D] - 1.0
        if i >= RET72H and closes[i - RET72H] > 0:
            ret72[i] = closes[i] / closes[i - RET72H] - 1.0
    return HourlyAux(atr_pct, vol_pct, ret30, ret72)
