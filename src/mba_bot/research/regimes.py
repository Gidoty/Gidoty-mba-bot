"""Descriptive regime labels for trades, so results can be split by the conditions they happened in.

These are LABELS FOR REPORTING, not trading signals, and nothing here feeds a decision. They use only
information available at the trade's entry (a trailing return, the entry ADX) except the volatility
terciles, which rank the entry's ATR% against the pair's whole sample (descriptive only).
"""
from __future__ import annotations

import bisect
from datetime import datetime, timezone

from ..indicators import atr
from ..strategy import Candle
from .engine import TradeRecord

TREND_ADX = 25.0
RANGE_ADX = 20.0
MOVE_THRESHOLD = 0.15  # +/-15% over the trailing 30 days
LOOKBACK_HOURS = 720


def _atr_pct_series(hourly: list[Candle], period: int = 14) -> list[float | None]:
    a = atr([c.high for c in hourly], [c.low for c in hourly], [c.close for c in hourly], period)
    return [(x / c.close) if (x is not None and c.close) else None for x, c in zip(a, hourly)]


def label_trades(trades: list[TradeRecord], hourly_by_symbol: dict[str, list[Candle]]) -> None:
    """Fills `trade.tags` in place."""
    prepared = {}
    for sym, hourly in hourly_by_symbol.items():
        series = _atr_pct_series(hourly)
        vals = sorted(v for v in series if v is not None)
        cuts = (vals[len(vals) // 3], vals[2 * len(vals) // 3]) if len(vals) >= 3 else (None, None)
        prepared[sym] = ([c.timestamp for c in hourly], [c.close for c in hourly], cuts)
    for t in trades:
        tags: dict[str, str] = {}
        adx = t.features.get("adx14")
        tags["trend_state"] = "unknown" if adx is None else ("trending" if adx >= TREND_ADX else "ranging" if adx < RANGE_ADX else "transition")
        info = prepared.get(t.symbol)
        if info is not None:
            ts, closes, cuts = info
            i = bisect.bisect_right(ts, t.entry_ts) - 1
            if i >= LOOKBACK_HOURS and closes[i - LOOKBACK_HOURS] > 0:
                ret = closes[i] / closes[i - LOOKBACK_HOURS] - 1.0
                tags["market_30d"] = "up" if ret > MOVE_THRESHOLD else "down" if ret < -MOVE_THRESHOLD else "flat"
            else:
                tags["market_30d"] = "unknown"
            if t.atr_pct is not None and cuts[0] is not None:
                tags["volatility"] = "low" if t.atr_pct <= cuts[0] else "high" if t.atr_pct > cuts[1] else "mid"
        d = datetime.fromtimestamp(t.entry_ts / 1000, tz=timezone.utc)
        tags["session"] = "asia_00_08" if d.hour < 8 else "europe_08_16" if d.hour < 16 else "us_16_24"
        tags["day_type"] = "weekend" if d.weekday() >= 5 else "weekday"
        t.tags = tags
