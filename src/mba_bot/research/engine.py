"""Backtester V2: replays the production decision path against stored candles.

What is REUSED from production, unmodified, so the research cannot drift from what trades:
  strategy.generate_signal        the EMA20/50 + ATR signal (same 200-bar window, same forming-candle input)
  pair_selection.rank_candidates  Auto Select's ranking
  risk.compute_stop_loss / compute_position_size / apply_exposure_ceiling / apply_margin_ceiling
  venue.FuturesVenue.plan_entry_quantity / round_stop and rules_from_market  (exchange filters)

What is MODELLED, because history cannot supply it (every number is a parameter you can change):
  taker fees on every fill, slippage (market and stop-market), funding payments at the
  recorded funding times, intrabar stop triggers from candle highs/lows, gaps through a stop.

TWO EXECUTION MODES
  production_replay  What the service does TODAY. It polls every 5 minutes and feeds
                     the strategy the still-FORMING hourly candle (built here from 5-minute
                     bars up to the poll time). A crossover can therefore appear and vanish
                     within the hour, and the same crossover is seen on every poll of that
                     hour. Needs 5-minute data.
  closed_candle      The signal is read only from a CLOSED hourly candle and executed one
                     poll later. This is a research mode for comparing against the above; it
                     is not what the service does. Works with 5-minute data (execution at the
                     first poll of the next hour) or hourly-only data (execution at the next
                     bar's open).

KNOWN LIMITS (also written into every report): historical exchange filters are unavailable (the
latest snapshot is used); liquidation and margin calls are not simulated, only counted when a stop
sits beyond ~80% of the liquidation distance; poll phase is fixed to 5-minute boundaries; stop
triggers assume the last-price trigger; a stop-hit bar's favourable excursion is not credited.
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field
from typing import Any

from ..features import compute_features
from ..pair_selection import PairCandidate, rank_candidates
from ..risk import (
    InvalidRiskParameters,
    apply_exposure_ceiling,
    apply_margin_ceiling,
    compute_position_size,
    compute_stop_loss,
)
from ..strategy import Candle, SignalType, StrategySignal, generate_signal
from ..venue import FuturesVenue, MarketRules

HOUR = 3_600_000
MODE_REPLAY = "production_replay"
MODE_CLOSED = "closed_candle"


class BacktestError(RuntimeError):
    pass


# ------------------------------------------------------------------ configuration


@dataclass(frozen=True)
class StrategyParams:
    """The knobs the production strategy reads (config.Settings). Defaults are production's."""

    ema_fast_period: int = 20
    ema_slow_period: int = 50
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0
    atr_flat_threshold: float = 0.75
    atr_rolling_window: int = 50
    ohlcv_limit: int = 200

    @classmethod
    def from_settings(cls, settings) -> "StrategyParams":
        return cls(
            ema_fast_period=settings.ema_fast_period, ema_slow_period=settings.ema_slow_period, atr_period=settings.atr_period,
            atr_stop_multiplier=settings.atr_stop_multiplier, atr_flat_threshold=settings.atr_flat_threshold,
            atr_rolling_window=settings.atr_rolling_window, ohlcv_limit=settings.ohlcv_limit,
        )


@dataclass(frozen=True)
class AccountParams:
    allocated_capital: float = 1000.0
    starting_equity: float | None = None  # defaults to allocated_capital
    risk_pct: float = 1.0
    max_risk_pct: float = 2.0
    max_notional_multiplier: float = 3.0
    leverage: float = 5.0  # the account's configured symbol leverage (production reads it from Binance)


@dataclass(frozen=True)
class CostModel:
    """Assumptions, not measurements. Defaults: standard Futures taker fee (verify your tier), and
    slippage chosen to be plausible for liquid USDT perpetuals; run the sensitivity multiples."""

    taker_fee_bps: float = 5.0
    market_slip_bps: float = 3.0
    stop_slip_bps: float = 8.0
    per_symbol_slip_bps: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {"BTCUSDT": (1.0, 3.0), "ETHUSDT": (1.5, 4.0)}
    )
    funding: bool = True
    multiplier: float = 1.0  # scales fees and slippage together (cost-stress runs)

    def fee(self) -> float:
        return self.taker_fee_bps * self.multiplier / 1e4

    def slip(self, symbol: str) -> tuple[float, float]:
        market, stop = self.per_symbol_slip_bps.get(symbol, (self.market_slip_bps, self.stop_slip_bps))
        return market * self.multiplier / 1e4, stop * self.multiplier / 1e4


@dataclass(frozen=True)
class BacktestConfig:
    mode: str = MODE_REPLAY
    strategy: StrategyParams = field(default_factory=StrategyParams)
    account: AccountParams = field(default_factory=AccountParams)
    costs: CostModel = field(default_factory=CostModel)
    auto: bool = False  # True: rank the whole universe like Auto Select; False: one fixed pair
    allow_short: bool = True
    start_ts: int | None = None  # inclusive, ms
    end_ts: int | None = None  # exclusive, ms
    warmup_bars: int | None = None  # hourly bars of history required before the first decision


@dataclass(frozen=True)
class PairData:
    symbol: str
    hourly: list[Candle]
    exec_bars: list[Candle] | None  # 5-minute bars, or None for hourly-only execution
    funding: list[tuple[int, float]]
    rules: MarketRules | None


# ------------------------------------------------------------------ results


@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry_ts: int
    exit_ts: int
    signal_price: float
    entry_price: float
    exit_price: float
    stop_price: float
    quantity: float
    notional: float
    planned_risk: float
    gross_pnl: float  # at reference prices, before any cost
    slippage_cost: float
    fees: float
    funding: float  # positive = paid
    net_pnl: float
    r_net: float
    r_gross: float
    mfe_r: float
    mae_r: float
    hold_hours: float
    exit_reason: str
    entry_signal: str
    equity_before: float
    signal_confirmed_at_close: bool | None = None
    same_hour_reentry: bool = False
    liquidation_risk: bool = False
    atr_pct: float | None = None
    features: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)  # regime labels added by the reporting layer


@dataclass
class RunResult:
    config: dict
    symbols: list[str]
    start_equity: float
    end_equity: float
    trades: list[TradeRecord]
    equity: list[tuple[int, float]]  # hourly mark-to-market
    counters: dict[str, int]
    span: tuple[int, int]
    notes: list[str] = field(default_factory=list)

    def counted_trades(self) -> list[TradeRecord]:
        """Trades that closed for a real reason (not the forced close at the end of the data)."""
        return [t for t in self.trades if t.exit_reason != "end_of_data"]


# ------------------------------------------------------------------ internals


@dataclass
class _Position:
    symbol: str
    side: str
    qty: float
    signal_price: float
    entry_fill: float
    stop: float
    stop_raw: float
    planned_risk: float
    entry_ts: int
    fee_entry: float
    slip_entry: float
    equity_before: float
    entry_signal: str
    atr_pct: float | None
    features: dict
    liquidation_risk: bool
    same_hour_reentry: bool
    funding_paid: float = 0.0
    fund_idx: int = 0
    best: float = 0.0
    worst: float = 0.0


def _rules_venue(rules: MarketRules) -> FuturesVenue:
    """A FuturesVenue carrying only the exchange rules, so the PRODUCTION
    plan_entry_quantity / round_stop run unchanged (no client, no network)."""
    venue = FuturesVenue.__new__(FuturesVenue)
    venue.rules = rules
    return venue


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


class _Engine:
    def __init__(self, pairs: dict[str, PairData], config: BacktestConfig):
        self.cfg = config
        self.pairs = pairs
        self.symbols = list(pairs)
        s = config.strategy
        self.sp = dict(
            ema_fast_period=s.ema_fast_period, ema_slow_period=s.ema_slow_period, atr_period=s.atr_period,
            atr_flat_threshold=s.atr_flat_threshold, atr_rolling_window=s.atr_rolling_window,
        )
        a = config.account
        self.equity = a.starting_equity if a.starting_equity is not None else a.allocated_capital
        self.start_equity = self.equity
        self.pos: _Position | None = None
        self.trades: list[TradeRecord] = []
        self.curve: list[tuple[int, float]] = []
        self.counters: dict[str, int] = {
            "polls": 0, "actionable_signals": 0, "entries": 0, "blocked_exchange_filters": 0, "blocked_size_zero": 0,
            "blocked_no_rules": 0, "blocked_no_stop_order": 0, "same_hour_reentries": 0, "liquidation_risk_entries": 0,
        }
        self.notes: list[str] = []
        self.last_exit: tuple[str, int] | None = None  # (symbol, hour of exit)
        self.venues = {sym: _rules_venue(p.rules) for sym, p in pairs.items() if p.rules is not None}
        self.hour_ts = {sym: [c.timestamp for c in p.hourly] for sym, p in pairs.items()}
        self.hour_pos = {sym: {t: i for i, t in enumerate(ts)} for sym, ts in self.hour_ts.items()}

    # -------- signals

    def _signal(self, window: list[Candle]) -> StrategySignal:
        return generate_signal(window, **self.sp)

    def _closed_window(self, sym: str, last_idx: int) -> list[Candle]:
        """The hourly candles ending at index `last_idx` (inclusive) - at most ohlcv_limit of them."""
        hourly = self.pairs[sym].hourly
        lo = max(0, last_idx + 1 - self.cfg.strategy.ohlcv_limit)
        return hourly[lo : last_idx + 1]

    def _side_for(self, sig: StrategySignal) -> str | None:
        if sig.signal == SignalType.BULLISH_CROSSOVER:
            return "long"
        if sig.signal == SignalType.BEARISH_CROSSOVER and self.cfg.allow_short:
            return "short"
        return None

    # -------- costs / fills

    def _buy(self, ref: float, slip: float) -> float:
        return ref * (1.0 + slip)

    def _sell(self, ref: float, slip: float) -> float:
        return ref * (1.0 - slip)

    # -------- entry (mirrors trader._maybe_enter's live Futures branch)

    def _try_enter(self, sym: str, sig: StrategySignal, price: float, ts: int, window: list[Candle]) -> bool:
        side = self._side_for(sig)
        if side is None or not sig.atr:
            return False
        venue = self.venues.get(sym)
        if venue is None:
            self.counters["blocked_no_rules"] += 1
            return False
        if "STOP_MARKET" not in venue.rules.order_types:
            self.counters["blocked_no_stop_order"] += 1
            return False
        acct, sp, costs = self.cfg.account, self.cfg.strategy, self.cfg.costs
        try:
            stop_raw = compute_stop_loss(entry_price=price, atr=sig.atr, side=side, atr_multiplier=sp.atr_stop_multiplier)
            effective_capital = min(acct.allocated_capital, self.equity)
            if effective_capital <= 0:
                return False
            size = compute_position_size(
                account_balance=effective_capital, risk_pct=acct.risk_pct, entry_price=price, stop_price=stop_raw,
                max_risk_pct=acct.max_risk_pct,
            )
            if size.quantity <= 0:
                self.counters["blocked_size_zero"] += 1
                return False
            size = apply_exposure_ceiling(
                size, entry_price=price, effective_capital=effective_capital, max_notional_multiplier=acct.max_notional_multiplier
            )
            size = apply_margin_ceiling(size, entry_price=price, effective_capital=effective_capital, leverage=acct.leverage)
        except InvalidRiskParameters:
            self.counters["blocked_size_zero"] += 1
            return False
        if size.quantity <= 0:
            self.counters["blocked_size_zero"] += 1
            return False
        qty = venue.plan_entry_quantity(size.quantity, entry_price=price, stop_price=stop_raw, side=side)
        if qty <= 0:
            self.counters["blocked_exchange_filters"] += 1
            return False
        stop = venue.round_stop(stop_raw, side=side)
        market_slip, _ = costs.slip(sym)
        fill = self._buy(price, market_slip) if side == "long" else self._sell(price, market_slip)
        fee = costs.fee() * qty * fill
        slip_cost = qty * abs(fill - price)
        stop_dist_pct = abs(price - stop_raw) / price
        liq = stop_dist_pct > 0.8 / acct.leverage
        same_hour = self.last_exit is not None and self.last_exit == (sym, (ts - 1) // HOUR * HOUR)
        feats = compute_features(
            window, ema_fast_period=sp.ema_fast_period, ema_slow_period=sp.ema_slow_period, atr_period=sp.atr_period,
            atr_rolling_window=sp.atr_rolling_window, now_ms=ts,
        )
        feats["bar_progress_at_entry"] = feats.pop("last_bar_progress", None)
        fund = self.pairs[sym].funding
        self.pos = _Position(
            symbol=sym, side=side, qty=qty, signal_price=price, entry_fill=fill, stop=stop, stop_raw=stop_raw,
            planned_risk=qty * abs(price - stop_raw), entry_ts=ts, fee_entry=fee, slip_entry=slip_cost,
            equity_before=self.equity, entry_signal=sig.signal.value, atr_pct=sig.atr_pct, features=feats,
            liquidation_risk=liq, same_hour_reentry=same_hour, fund_idx=bisect.bisect_right([f[0] for f in fund], ts),
            best=price, worst=price,
        )
        self.counters["entries"] += 1
        if same_hour:
            self.counters["same_hour_reentries"] += 1
        if liq:
            self.counters["liquidation_risk_entries"] += 1
        return True

    # -------- exit

    def _close(self, ref_exit: float, fill_exit: float, ts: int, reason: str) -> None:
        p = self.pos
        assert p is not None
        costs = self.cfg.costs
        sgn = _sign(p.side)
        gross = (ref_exit - p.signal_price) * p.qty * sgn
        slip_exit = p.qty * abs(fill_exit - ref_exit)
        fee_exit = costs.fee() * p.qty * fill_exit
        fees = p.fee_entry + fee_exit
        slip = p.slip_entry + slip_exit
        net = gross - slip - fees - p.funding_paid
        risk = p.planned_risk or 1e-12
        # best/worst are tracked as prices; convert to R against the planned stop distance.
        per_unit = abs(p.signal_price - p.stop_raw) or 1e-12
        if p.side == "long":
            mfe, mae = (p.best - p.signal_price) / per_unit, (p.signal_price - p.worst) / per_unit
        else:
            mfe, mae = (p.signal_price - p.worst) / per_unit, (p.best - p.signal_price) / per_unit
        self.trades.append(
            TradeRecord(
                symbol=p.symbol, side=p.side, entry_ts=p.entry_ts, exit_ts=ts, signal_price=p.signal_price,
                entry_price=p.entry_fill, exit_price=fill_exit, stop_price=p.stop, quantity=p.qty,
                notional=p.qty * p.signal_price, planned_risk=p.planned_risk, gross_pnl=gross, slippage_cost=slip, fees=fees,
                funding=p.funding_paid, net_pnl=net, r_net=net / risk, r_gross=gross / risk, mfe_r=max(mfe, 0.0), mae_r=max(mae, 0.0),
                hold_hours=(ts - p.entry_ts) / HOUR, exit_reason=reason, entry_signal=p.entry_signal,
                equity_before=p.equity_before, same_hour_reentry=p.same_hour_reentry, liquidation_risk=p.liquidation_risk,
                atr_pct=p.atr_pct, features=p.features,
            )
        )
        self.equity += net
        self.last_exit = (p.symbol, (ts - 1) // HOUR * HOUR)
        self.pos = None

    # -------- per-bar position upkeep

    def _apply_funding(self, bar_start: int, bar_end: int, mark: float) -> None:
        p = self.pos
        if p is None or not self.cfg.costs.funding:
            return
        fund = self.pairs[p.symbol].funding
        while p.fund_idx < len(fund) and fund[p.fund_idx][0] < bar_end:
            t, rate = fund[p.fund_idx]
            p.fund_idx += 1
            if t > p.entry_ts:
                p.funding_paid += _sign(p.side) * rate * p.qty * mark  # longs pay a positive rate

    def _check_stop(self, bar: Candle, bar_end: int) -> bool:
        p = self.pos
        if p is None:
            return False
        _, stop_slip = self.cfg.costs.slip(p.symbol)
        if p.side == "long" and bar.low <= p.stop:
            p.worst = min(p.worst, bar.low)  # the adverse side of a stop-hit bar is known; the favourable side is not
            ref = min(bar.open, p.stop)  # a gap open below the stop fills at the open
            self._close(ref, self._sell(ref, stop_slip), bar_end, "stop_loss")
            return True
        if p.side == "short" and bar.high >= p.stop:
            p.best = max(p.best, bar.high)
            ref = max(bar.open, p.stop)
            self._close(ref, self._buy(ref, stop_slip), bar_end, "stop_loss")
            return True
        return False

    def _track_excursion(self, bar: Candle) -> None:
        p = self.pos
        if p is None:
            return
        p.best = max(p.best, bar.high)
        p.worst = min(p.worst, bar.low)

    def _market_exit(self, price: float, ts: int, reason: str) -> None:
        p = self.pos
        assert p is not None
        slip, _ = self.cfg.costs.slip(p.symbol)
        fill = self._sell(price, slip) if p.side == "long" else self._buy(price, slip)
        self._close(price, fill, ts, reason)

    # -------- decisions

    def _is_opposite(self, side: str, sig: StrategySignal) -> bool:
        return (side == "long" and sig.signal == SignalType.BEARISH_CROSSOVER) or (
            side == "short" and sig.signal == SignalType.BULLISH_CROSSOVER
        )

    def _decide(self, windows: dict[str, list[Candle]], prices: dict[str, float], ts: int) -> None:
        """One poll's worth of decisions on the supplied windows (production's `process_account`)."""
        self.counters["polls"] += 1
        if self.pos is not None:
            sym = self.pos.symbol
            sig = self._signal(windows[sym])
            if self._is_opposite(self.pos.side, sig):
                self._market_exit(prices[sym], ts, "crossover_exit")
            return
        if self.cfg.auto:
            candidates = [PairCandidate(pair=s, candles=w) for s, w in windows.items()]
            ranked = rank_candidates(candidates, market_type="futures", **self.sp)
            if not ranked:
                return
            sym = ranked[0].pair
            sig = self._signal(windows[sym])
        else:
            sym = self.symbols[0]
            sig = self._signal(windows[sym])
        if self._side_for(sig) is None:
            return
        self.counters["actionable_signals"] += 1
        self._try_enter(sym, sig, prices[sym], ts, windows[sym])

    def _snapshot(self, ts: int, marks: dict[str, float]) -> None:
        eq = self.equity
        p = self.pos
        if p is not None:
            eq += (marks[p.symbol] - p.signal_price) * p.qty * _sign(p.side) - p.funding_paid - p.fee_entry - p.slip_entry
        self.curve.append((ts, eq))


# ------------------------------------------------------------------ the two walkers


def _common_timeline(pairs: dict[str, PairData], cfg: BacktestConfig, use_exec: bool) -> list[int]:
    sets = []
    for p in pairs.values():
        bars = p.exec_bars if use_exec else p.hourly
        sets.append({c.timestamp for c in bars})
    common = set.intersection(*sets)
    out = sorted(common)
    if cfg.start_ts is not None:
        out = [t for t in out if t >= cfg.start_ts]
    if cfg.end_ts is not None:
        out = [t for t in out if t < cfg.end_ts]
    return out


def run_backtest(pairs: dict[str, PairData], config: BacktestConfig) -> RunResult:
    if not pairs:
        raise BacktestError("no pairs supplied")
    if config.mode not in (MODE_REPLAY, MODE_CLOSED):
        raise BacktestError(f"unknown mode {config.mode!r}")
    if config.auto and len(pairs) < 2:
        raise BacktestError("auto mode needs a universe of at least two pairs")
    if not config.auto and len(pairs) != 1:
        raise BacktestError("fixed-pair mode takes exactly one pair")
    have_exec = all(p.exec_bars for p in pairs.values())
    if config.mode == MODE_REPLAY and not have_exec:
        raise BacktestError("production_replay needs 5-minute data for every pair (it replays the 5-minute polls)")
    engine = _Engine(pairs, config)
    if config.mode == MODE_REPLAY:
        _walk_replay(engine)
    elif have_exec:
        _walk_closed_5m(engine)
    else:
        _walk_closed_hourly(engine)
    _diagnose_confirmation(engine)
    if config.mode == MODE_REPLAY:
        engine.notes.append("production_replay: signals were read from the still-forming hourly candle at 5-minute polls")
    return RunResult(
        config=_config_dict(config), symbols=engine.symbols, start_equity=engine.start_equity, end_equity=engine.equity,
        trades=engine.trades, equity=engine.curve, counters=engine.counters,
        span=(engine.curve[0][0], engine.curve[-1][0]) if engine.curve else (0, 0), notes=engine.notes,
    )


def _config_dict(config: BacktestConfig) -> dict[str, Any]:
    return asdict(config)


# --------------------------- production_replay


def _walk_replay(e: _Engine) -> None:
    cfg = e.cfg
    limit = cfg.strategy.ohlcv_limit
    warm = cfg.warmup_bars if cfg.warmup_bars is not None else max(cfg.strategy.ema_slow_period + 5, cfg.strategy.atr_rolling_window + 5)
    timeline = _common_timeline(e.pairs, cfg, use_exec=True)
    if not timeline:
        raise BacktestError("the pairs share no 5-minute timestamps")
    step = 300_000
    bars = {s: {c.timestamp: c for c in p.exec_bars} for s, p in e.pairs.items()}
    e.curve.append((timeline[0], e.equity))
    forming: dict[str, list[float]] = {}  # symbol -> [hour_start, o, h, l, c, v]
    for ts in timeline:
        hs = ts - ts % HOUR
        end = ts + step
        prices: dict[str, float] = {}
        for sym in e.symbols:
            b = bars[sym][ts]
            f = forming.get(sym)
            if f is None or f[0] != hs:
                forming[sym] = [hs, b.open, b.high, b.low, b.close, b.volume]
            else:
                f[2] = max(f[2], b.high)
                f[3] = min(f[3], b.low)
                f[4] = b.close
                f[5] += b.volume
            prices[sym] = b.close
        if e.pos is not None:
            b = bars[e.pos.symbol][ts]
            e._apply_funding(ts, end, b.open)
            stopped = e._check_stop(b, end)
            if not stopped:
                e._track_excursion(b)
        # the poll at the end of this 5-minute bar
        ready = {}
        for sym in e.symbols:
            idx = e.hour_pos[sym].get(hs)
            if idx is None:
                continue
            if idx < warm:
                continue
            f = forming[sym]
            closed = e.pairs[sym].hourly[max(0, idx - (limit - 1)) : idx]
            ready[sym] = closed + [Candle(int(f[0]), f[1], f[2], f[3], f[4], f[5])]
        wanted = [e.pos.symbol] if e.pos is not None else e.symbols
        if all(s in ready for s in wanted):
            e._decide({s: ready[s] for s in wanted}, prices, end)
        if end % HOUR == 0:
            e._snapshot(end, prices)
    _force_close(e, timeline[-1] + step, {s: bars[s][timeline[-1]].close for s in e.symbols})


# --------------------------- closed_candle with 5-minute execution


def _walk_closed_5m(e: _Engine) -> None:
    cfg = e.cfg
    warm = cfg.warmup_bars if cfg.warmup_bars is not None else max(cfg.strategy.ema_slow_period + 5, cfg.strategy.atr_rolling_window + 5)
    timeline = _common_timeline(e.pairs, cfg, use_exec=True)
    if not timeline:
        raise BacktestError("the pairs share no 5-minute timestamps")
    step = 300_000
    bars = {s: {c.timestamp: c for c in p.exec_bars} for s, p in e.pairs.items()}
    e.curve.append((timeline[0], e.equity))
    pending: dict | None = None
    for ts in timeline:
        end = ts + step
        prices = {sym: bars[sym][ts].close for sym in e.symbols}
        if e.pos is not None:
            b = bars[e.pos.symbol][ts]
            e._apply_funding(ts, end, b.open)
            if not e._check_stop(b, end):
                e._track_excursion(b)
        # execute what the previous hour-close decided, at this poll
        if pending is not None:
            _execute_pending(e, pending, prices, end)
            pending = None
        if end % HOUR == 0:  # an hourly candle just closed: decide on it, execute next poll
            hs = ts - ts % HOUR
            windows = {}
            for sym in e.symbols:
                idx = e.hour_pos[sym].get(hs)
                if idx is not None and idx >= warm:
                    windows[sym] = e._closed_window(sym, idx)
            wanted = [e.pos.symbol] if e.pos is not None else e.symbols
            if all(s in windows for s in wanted):
                pending = _decide_closed(e, {s: windows[s] for s in wanted})
            e._snapshot(end, prices)
    _force_close(e, timeline[-1] + step, {s: bars[s][timeline[-1]].close for s in e.symbols})


# --------------------------- closed_candle with hourly-only execution


def _walk_closed_hourly(e: _Engine) -> None:
    cfg = e.cfg
    warm = cfg.warmup_bars if cfg.warmup_bars is not None else max(cfg.strategy.ema_slow_period + 5, cfg.strategy.atr_rolling_window + 5)
    timeline = _common_timeline(e.pairs, cfg, use_exec=False)
    if not timeline:
        raise BacktestError("the pairs share no hourly timestamps")
    e.notes.append("hourly-only execution: entries at the next bar's open; intrabar order of events inside a bar is unknown")
    bars = {s: {c.timestamp: c for c in p.hourly} for s, p in e.pairs.items()}
    e.curve.append((timeline[0], e.equity))
    pending: dict | None = None
    for ts in timeline:
        end = ts + HOUR
        opens = {sym: bars[sym][ts].open for sym in e.symbols}
        if pending is not None:
            _execute_pending(e, pending, opens, ts)
            pending = None
        if e.pos is not None:
            b = bars[e.pos.symbol][ts]
            e._apply_funding(ts, end, b.open)
            if not e._check_stop(b, end):
                e._track_excursion(b)
        closes = {sym: bars[sym][ts].close for sym in e.symbols}
        windows = {}
        for sym in e.symbols:
            idx = e.hour_pos[sym].get(ts)
            if idx is not None and idx >= warm:
                windows[sym] = e._closed_window(sym, idx)
        wanted = [e.pos.symbol] if e.pos is not None else e.symbols
        if all(s in windows for s in wanted):
            pending = _decide_closed(e, {s: windows[s] for s in wanted})
        e._snapshot(end, closes)
    _force_close(e, timeline[-1] + HOUR, {s: bars[s][timeline[-1]].close for s in e.symbols})


def _decide_closed(e: _Engine, windows: dict[str, list[Candle]]) -> dict | None:
    """The hour-close decision. Returned, not executed: execution happens at the next poll."""
    e.counters["polls"] += 1
    if e.pos is not None:
        sig = e._signal(windows[e.pos.symbol])
        if e._is_opposite(e.pos.side, sig):
            return {"kind": "exit", "symbol": e.pos.symbol}
        return None
    if e.cfg.auto:
        ranked = rank_candidates([PairCandidate(pair=s, candles=w) for s, w in windows.items()], market_type="futures", **e.sp)
        if not ranked:
            return None
        sym = ranked[0].pair
    else:
        sym = e.symbols[0]
    sig = e._signal(windows[sym])
    if e._side_for(sig) is None:
        return None
    e.counters["actionable_signals"] += 1
    return {"kind": "enter", "symbol": sym, "signal": sig, "window": windows[sym]}


def _execute_pending(e: _Engine, pending: dict, prices: dict[str, float], ts: int) -> None:
    if pending["kind"] == "exit":
        if e.pos is not None and e.pos.symbol == pending["symbol"]:
            e._market_exit(prices[pending["symbol"]], ts, "crossover_exit")
    elif e.pos is None:
        # ATR comes from the closed-candle signal; the stop is placed from the actual execution price
        e._try_enter(pending["symbol"], pending["signal"], prices[pending["symbol"]], ts, pending["window"])


def _force_close(e: _Engine, ts: int, prices: dict[str, float]) -> None:
    if e.pos is not None:
        e._market_exit(prices[e.pos.symbol], ts, "end_of_data")


# ------------------------------------------------------------------ diagnostics


def _diagnose_confirmation(e: _Engine) -> None:
    """Replay mode only. For each entry, was the signal STILL there once its hour had closed?
    Computed after the run from data the decision could not see; it never influences a trade."""
    if e.cfg.mode != MODE_REPLAY:
        return
    for t in e.trades:
        hs = (t.entry_ts - 1) // HOUR * HOUR
        idx = e.hour_pos[t.symbol].get(hs)
        if idx is None:
            continue
        sig = e._signal(e._closed_window(t.symbol, idx))
        t.signal_confirmed_at_close = sig.signal.value == t.entry_signal
