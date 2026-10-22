"""Hand-built price paths for the research engine tests (not market data)."""
from __future__ import annotations

from mba_bot.research import synthetic
from mba_bot.research.engine import AccountParams, BacktestConfig, CostModel, PairData, StrategyParams
from mba_bot.strategy import Candle
from mba_bot.venue import rules_from_market

HOUR = 3_600_000
FIVE = 300_000
T0 = 1_700_000_000_000 // HOUR * HOUR

# Tiny indicator periods so a handful of hourly bars is enough warm-up (production uses 20/50/14/50).
TINY = StrategyParams(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_stop_multiplier=2.0, atr_flat_threshold=0.75,
                      atr_rolling_window=3, ohlcv_limit=12)
FREE = CostModel(taker_fee_bps=0.0, market_slip_bps=0.0, stop_slip_bps=0.0, per_symbol_slip_bps={}, funding=False)
RICH = AccountParams(allocated_capital=100_000.0, risk_pct=1.0, max_risk_pct=2.0, max_notional_multiplier=1000.0, leverage=1000.0)


def rules(symbol="TESTUSDT", min_notional="5", step="0.001", tick="0.01"):
    info = synthetic.exchange_info_for([symbol])["symbols"][0]
    for f in info["filters"]:
        if f["filterType"] == "PRICE_FILTER":
            f["tickSize"] = f["minPrice"] = tick
        if f["filterType"] in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            f["stepSize"] = f["minQty"] = step
        if f["filterType"] == "MIN_NOTIONAL":
            f["notional"] = min_notional
    return rules_from_market({"info": info})


def bars_5m(hour_closes, *, start_price=None, wick=0.4, overrides=None, spikes=None, t0=T0):
    """Twelve 5-minute bars per hour moving linearly to each hour's close. `overrides` maps an hour
    index to an explicit list of twelve 5-minute CLOSES, used to script an intrahour path."""
    out: list[Candle] = []
    prev = start_price if start_price is not None else hour_closes[0]
    for h, target in enumerate(hour_closes):
        closes = (overrides or {}).get(h) or [prev + (target - prev) * (k + 1) / 12 for k in range(12)]
        if (overrides or {}).get(h):
            target = closes[-1]
        o = prev
        for k, c in enumerate(closes):
            lo, hi = (spikes or {}).get((h, k), (min(o, c) - wick, max(o, c) + wick))
            out.append(Candle(t0 + h * HOUR + k * FIVE, o, max(hi, o, c), min(lo, o, c), c, 10.0))
            o = c
        prev = closes[-1]
    return out


def to_hourly(b5):
    out = []
    for i in range(0, len(b5), 12):
        chunk = b5[i : i + 12]
        out.append(Candle(chunk[0].timestamp, chunk[0].open, max(c.high for c in chunk), min(c.low for c in chunk), chunk[-1].close, sum(c.volume for c in chunk)))
    return out


def pair(hour_closes, *, symbol="TESTUSDT", funding=None, overrides=None, spikes=None, wick=0.4, min_notional="5", with_5m=True, bars=None):
    b5 = bars if bars is not None else bars_5m(hour_closes, wick=wick, overrides=overrides, spikes=spikes)
    return PairData(symbol=symbol, hourly=to_hourly(b5), exec_bars=b5 if with_5m else None, funding=funding or [], rules=rules(symbol, min_notional=min_notional))


def config(mode, **kw):
    kw.setdefault("strategy", TINY)
    kw.setdefault("account", RICH)
    kw.setdefault("costs", FREE)
    return BacktestConfig(mode=mode, **kw)
