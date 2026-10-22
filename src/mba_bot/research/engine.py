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

PHASE 1 ADDITIONS (research only; the defaults reproduce Phase 0 exactly, pinned by a golden test)
  Policy       optional entry filters, exits and Auto scoring that production does NOT have. Every field
               defaults to "off". They exist so a candidate change can be measured before anyone builds it.
  SignalTape   the production signal per poll, computed once and shared (see tape.py), so an experiment
               costs seconds instead of minutes.

KNOWN LIMITS (also written into every report): historical exchange filters are unavailable (the
latest snapshot is used); liquidation and margin calls are not simulated, only counted when a stop
sits beyond ~80% of the liquidation distance; poll phase is fixed to 5-minute boundaries; stop
triggers assume the last-price trigger; a stop-hit bar's favourable excursion is not credited.
"""
from __future__ import annotations

import bisect
import random
from dataclasses import asdict, dataclass, field

from ..features import compute_features
from ..risk import (
    InvalidRiskParameters,
    apply_exposure_ceiling,
    apply_margin_ceiling,
    compute_position_size,
    compute_stop_loss,
)
from ..strategy import Candle, SignalType, StrategySignal
from ..venue import FuturesVenue, MarketRules
from . import tape as tapelib
from .tape import CODE_BEAR, CODE_BULL, CODE_NA, HourlyAux, SignalTape

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
class Policy:
    """Research-only rules layered on the production decision path. All off by default."""

    # H1: entry confirmation (replay mode). `confirm_sides` says which sides the rules apply to.
    confirm_sides: tuple[str, ...] = ("long", "short")
    confirm_polls: int = 0  # the same actionable signal on this many consecutive scanning polls
    confirm_margin_atr: float = 0.0  # |fast - slow| / ATR at the poll must be at least this
    wait_for_close_sides: tuple[str, ...] = ()  # these sides enter only on a CLOSED hourly candle, executed one poll later
    # direction
    long_allowed: bool = True
    short_allowed: bool = True
    short_max_ret30d: float | None = None  # shorts only while the trailing 30-day return is at or below this
    short_risk_scale: float = 1.0  # Phase 2: shorts are sized at this fraction of the normal risk (1.0 = production)
    # H2: volatility (causal percentile of the pair's own ATR% over the trailing 90 days)
    vol_skip_above: float | None = None
    vol_half_risk_above: float | None = None
    # H4: exits (the stop only ever tightens; it moves at a poll and applies from the next bar)
    exit_on_close_only: bool = False  # the strategy's opposite-crossover exit is acted on only when the hourly candle closes
    breakeven_at_r: float | None = None
    trail_atr_mult: float | None = None  # trailing distance in entry-ATR multiples from the best price seen
    trail_start_r: float = 1.0  # the trail arms once the trade has been this many R in profit
    # H5: Auto Select scoring: production | random | calm | momentum | composite
    auto_score: str = "production"
    auto_seed: int = 0

    def is_default(self) -> bool:
        return self == Policy()


@dataclass(frozen=True)
class BacktestConfig:
    mode: str = MODE_REPLAY
    strategy: StrategyParams = field(default_factory=StrategyParams)
    account: AccountParams = field(default_factory=AccountParams)
    costs: CostModel = field(default_factory=CostModel)
    auto: bool = False  # True: rank the whole universe like Auto Select; False: one fixed pair
    allow_short: bool = True
    policy: Policy = field(default_factory=Policy)
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
    decisions: list[dict] = field(default_factory=list)  # Auto entry decisions with 2+ eligible candidates

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
    atr_entry: float
    features: dict
    liquidation_risk: bool
    same_hour_reentry: bool
    funding_paid: float = 0.0
    fund_idx: int = 0
    best: float = 0.0
    worst: float = 0.0
    stop_kind: str | None = None
    stop_moves: int = 0


def _rules_venue(rules: MarketRules) -> FuturesVenue:
    """A FuturesVenue carrying only the exchange rules, so the PRODUCTION
    plan_entry_quantity / round_stop run unchanged (no client, no network)."""
    venue = FuturesVenue.__new__(FuturesVenue)
    venue.rules = rules
    return venue


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


AUTO_SCORES = ("production", "random", "calm", "momentum", "composite")


def production_score(sig: StrategySignal) -> float:
    """Auto Select's ranking score (pair_selection.rank_candidates): EMA separation in ATR units."""
    return abs(sig.fast_ema - sig.slow_ema) / sig.atr


class _Engine:
    def __init__(self, pairs: dict[str, PairData], config: BacktestConfig, aux: dict[str, HourlyAux]):
        self.cfg = config
        self.pol = config.policy
        self.pairs = pairs
        self.aux = aux
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
            "poll_rejections_direction": 0, "poll_rejections_short_trend": 0, "poll_rejections_margin": 0,
            "poll_rejections_persistence": 0, "poll_rejections_volatility": 0, "deferred_to_candle_close": 0,
            "half_risk_entries": 0, "stop_moves": 0,
            "decision_polls_1_candidate": 0, "decision_polls_2_candidates": 0, "decision_polls_3plus_candidates": 0,
        }
        self.decision_log: list[dict] = []  # Auto decisions with two or more eligible candidates
        self.notes: list[str] = []
        self.last_exit: tuple[str, int] | None = None  # (symbol, hour of exit)
        self.venues = {sym: _rules_venue(p.rules) for sym, p in pairs.items() if p.rules is not None}
        self.hour_pos = {sym: {c.timestamp: i for i, c in enumerate(p.hourly)} for sym, p in pairs.items()}
        self.runs: dict[str, tuple[SignalType, int]] = {}  # consecutive scanning polls with the same actionable signal
        self.pending: dict | None = None
        self._rng_cache: dict = {}

    # -------- signals

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

    # -------- policy: is this signal allowed to become an entry?

    def _eligible(self, sym: str, sig: StrategySignal, side: str, aux_idx: int | None, run_n: int, *, replay: bool) -> tuple[bool, float]:
        """Returns (allowed, risk_scale). Rules are checked in a fixed order and the first failure is counted."""
        pol, c = self.pol, self.counters
        if (side == "long" and not pol.long_allowed) or (side == "short" and not pol.short_allowed):
            c["poll_rejections_direction"] += 1
            return False, 1.0
        aux = self.aux[sym]
        if side == "short" and pol.short_max_ret30d is not None and aux_idx is not None:
            r = aux.ret30d[aux_idx]
            if r is not None and r > pol.short_max_ret30d:
                c["poll_rejections_short_trend"] += 1
                return False, 1.0
        if side in pol.confirm_sides:
            if pol.confirm_margin_atr > 0 and sig.atr:
                if abs(sig.fast_ema - sig.slow_ema) / sig.atr < pol.confirm_margin_atr:
                    c["poll_rejections_margin"] += 1
                    return False, 1.0
            if replay and pol.confirm_polls > 1 and run_n < pol.confirm_polls:
                c["poll_rejections_persistence"] += 1
                return False, 1.0
        scale = 1.0
        v = aux.vol_pct[aux_idx] if aux_idx is not None else None
        if v is not None:
            if pol.vol_skip_above is not None and v > pol.vol_skip_above:
                c["poll_rejections_volatility"] += 1
                return False, 1.0
            if pol.vol_half_risk_above is not None and v > pol.vol_half_risk_above:
                scale = 0.5
        if side == "short" and pol.short_risk_scale < 1.0:
            scale *= pol.short_risk_scale
        return True, scale

    def _count_candidates(self, n: int) -> None:
        key = "decision_polls_1_candidate" if n == 1 else ("decision_polls_2_candidates" if n == 2 else "decision_polls_3plus_candidates")
        self.counters[key] += 1

    def _extras(self, sym: str, sig: StrategySignal, aux_idx: int | None, run_n: int, n_cand: int = 1) -> dict:
        aux = self.aux[sym]
        gap = abs(sig.fast_ema - sig.slow_ema) / sig.atr if (sig.atr and sig.fast_ema is not None) else None
        return {
            "gap_atr_at_entry": gap, "persist_polls": run_n, "n_candidates": n_cand,
            "vol_pct_90d": aux.vol_pct[aux_idx] if aux_idx is not None else None,
            "ret30d": aux.ret30d[aux_idx] if aux_idx is not None else None,
            "ret72h": aux.ret72h[aux_idx] if aux_idx is not None else None,
        }

    # -------- Auto Select scoring

    def _pick(self, elig: list[tuple], ts: int) -> tuple:
        """`elig` rows: (symbol, signal, side, aux_idx, run_n, risk_scale). Production keeps list order for ties."""
        mode = self.pol.auto_score
        if mode == "production":
            return sorted(elig, key=lambda r: production_score(r[1]), reverse=True)[0]
        if mode == "random":
            return random.Random(f"{self.pol.auto_seed}|{ts}").choice(elig)
        calm = {r[0]: 1.0 - (self.aux[r[0]].vol_pct[r[3]] if (r[3] is not None and self.aux[r[0]].vol_pct[r[3]] is not None) else 0.5) for r in elig}
        mom = {}
        for r in elig:
            a = self.aux[r[0]]
            ret = a.ret72h[r[3]] if r[3] is not None else None
            apct = r[1].atr_pct or 0.0
            mom[r[0]] = (_sign(r[2]) * ret / apct) if (ret is not None and apct) else 0.0
        if mode == "calm":
            return sorted(elig, key=lambda r: (calm[r[0]], production_score(r[1])), reverse=True)[0]
        if mode == "momentum":
            return sorted(elig, key=lambda r: (mom[r[0]], production_score(r[1])), reverse=True)[0]
        # composite: average of the two ranks (a higher value is better in each)
        rc = {r[0]: i for i, r in enumerate(sorted(elig, key=lambda r: calm[r[0]]))}
        rm = {r[0]: i for i, r in enumerate(sorted(elig, key=lambda r: mom[r[0]]))}
        return sorted(elig, key=lambda r: (rc[r[0]] + rm[r[0]], production_score(r[1])), reverse=True)[0]

    # -------- entry (mirrors trader._maybe_enter's live Futures branch)

    def _try_enter(self, sym: str, sig: StrategySignal, price: float, ts: int, window_fn, *, risk_scale: float = 1.0, extra: dict | None = None) -> bool:
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
                account_balance=effective_capital, risk_pct=acct.risk_pct * risk_scale, entry_price=price, stop_price=stop_raw,
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
            window_fn(), ema_fast_period=sp.ema_fast_period, ema_slow_period=sp.ema_slow_period, atr_period=sp.atr_period,
            atr_rolling_window=sp.atr_rolling_window, now_ms=ts,
        )
        feats["bar_progress_at_entry"] = feats.pop("last_bar_progress", None)
        if extra:
            feats.update(extra)
        fund = self.pairs[sym].funding
        self.pos = _Position(
            symbol=sym, side=side, qty=qty, signal_price=price, entry_fill=fill, stop=stop, stop_raw=stop_raw,
            planned_risk=qty * abs(price - stop_raw), entry_ts=ts, fee_entry=fee, slip_entry=slip_cost,
            equity_before=self.equity, entry_signal=sig.signal.value, atr_pct=sig.atr_pct, atr_entry=sig.atr, features=feats,
            liquidation_risk=liq, same_hour_reentry=same_hour, fund_idx=bisect.bisect_right([f[0] for f in fund], ts),
            best=price, worst=price,
        )
        self.runs.clear()
        self.counters["entries"] += 1
        if risk_scale < 1.0:
            self.counters["half_risk_entries"] += 1
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
        if p.stop_moves:
            p.features["stop_moves"] = p.stop_moves
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
        reason = p.stop_kind or "stop_loss"
        if p.side == "long" and bar.low <= p.stop:
            p.worst = min(p.worst, bar.low)  # the adverse side of a stop-hit bar is known; the favourable side is not
            ref = min(bar.open, p.stop)  # a gap open below the stop fills at the open
            self._close(ref, self._sell(ref, stop_slip), bar_end, reason)
            return True
        if p.side == "short" and bar.high >= p.stop:
            p.best = max(p.best, bar.high)
            ref = max(bar.open, p.stop)
            self._close(ref, self._buy(ref, stop_slip), bar_end, reason)
            return True
        return False

    def _track_excursion(self, bar: Candle) -> None:
        p = self.pos
        if p is None:
            return
        p.best = max(p.best, bar.high)
        p.worst = min(p.worst, bar.low)

    def _update_stops(self, last_close: float) -> None:
        """Breakeven and trailing stop. Evaluated at a poll from bars already completed; the new stop applies
        from the NEXT bar, as a cancel-and-replace at the exchange would. It only ever moves toward the price."""
        pol, p = self.pol, self.pos
        if p is None or (pol.breakeven_at_r is None and pol.trail_atr_mult is None):
            return
        long = p.side == "long"
        per_unit = abs(p.signal_price - p.stop_raw) or 1e-12
        mfe_r = ((p.best - p.signal_price) if long else (p.signal_price - p.worst)) / per_unit
        venue = self.venues.get(p.symbol)
        if venue is None:
            return
        levels: list[tuple[float, str]] = []
        fee = self.cfg.costs.fee()
        if pol.breakeven_at_r is not None and mfe_r >= pol.breakeven_at_r:
            levels.append((p.entry_fill * (1 + 2 * fee) if long else p.entry_fill * (1 - 2 * fee), "breakeven_stop"))
        if pol.trail_atr_mult is not None and mfe_r >= pol.trail_start_r and p.atr_entry:
            dist = pol.trail_atr_mult * p.atr_entry
            levels.append((p.best - dist if long else p.worst + dist, "trailing_stop"))
        for level, kind in sorted(levels, key=lambda x: x[0], reverse=long):
            level = venue.round_stop(level, side=p.side)
            if long and p.stop < level < last_close or (not long) and last_close < level < p.stop:
                p.stop, p.stop_kind = level, kind
                p.stop_moves += 1
                self.counters["stop_moves"] += 1
                return

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

    def _scan(self, sigs: dict[str, StrategySignal], aux_idx: dict[str, int | None], *, replay: bool) -> list[tuple]:
        """Eligible entry candidates among `sigs`: rows (symbol, signal, side, aux_idx, run_n, risk_scale)."""
        elig = []
        for sym, sig in sigs.items():
            side = self._side_for(sig)
            if side is None:
                self.runs.pop(sym, None)
                continue
            last = self.runs.get(sym)
            n = last[1] + 1 if (last is not None and last[0] == sig.signal) else 1
            self.runs[sym] = (sig.signal, n)
            if self.cfg.auto and not sig.atr:
                continue
            ok, scale = self._eligible(sym, sig, side, aux_idx.get(sym), n, replay=replay)
            if ok:
                elig.append((sym, sig, side, aux_idx.get(sym), n, scale))
        return elig

    def _decide_replay(self, sigs: dict[str, StrategySignal], prices: dict[str, float], ts: int, aux_idx: dict[str, int | None],
                       window_fn, complete_hour: bool) -> None:
        """One poll's worth of decisions (production's `process_account`)."""
        self.counters["polls"] += 1
        if self.pos is not None:
            sym = self.pos.symbol
            if self._is_opposite(self.pos.side, sigs[sym]) and (complete_hour or not self.pol.exit_on_close_only):
                self._market_exit(prices[sym], ts, "crossover_exit")
            return
        elig = self._scan(sigs, aux_idx, replay=True)
        if not elig:
            return
        sym, sig, side, ai, n, scale = self._pick(elig, ts) if self.cfg.auto else elig[0]
        self.counters["actionable_signals"] += 1
        self._count_candidates(len(elig))
        if side in self.pol.wait_for_close_sides:
            if not complete_hour:
                self.counters["deferred_to_candle_close"] += 1
                return
            self.pending = {"kind": "enter", "symbol": sym, "signal": sig, "window_fn": window_fn(sym), "scale": scale,
                            "extra": self._extras(sym, sig, ai, n, len(elig))}
            return
        entered = self._try_enter(sym, sig, prices[sym], ts, window_fn(sym), risk_scale=scale, extra=self._extras(sym, sig, ai, n, len(elig)))
        if self.cfg.auto and len(elig) >= 2:
            self.decision_log.append({"ts": ts, "chosen": sym, "candidates": [r[0] for r in elig], "entered": entered})

    def _decide_closed(self, sigs: dict[str, StrategySignal], aux_idx: dict[str, int | None], window_fn, ts: int) -> dict | None:
        """The hour-close decision. Returned, not executed: execution happens at the next poll."""
        self.counters["polls"] += 1
        if self.pos is not None:
            if self._is_opposite(self.pos.side, sigs[self.pos.symbol]):
                return {"kind": "exit", "symbol": self.pos.symbol}
            return None
        elig = self._scan(sigs, aux_idx, replay=False)
        if not elig:
            return None
        sym, sig, side, ai, n, scale = self._pick(elig, ts) if self.cfg.auto else elig[0]
        self.counters["actionable_signals"] += 1
        self._count_candidates(len(elig))
        if self.cfg.auto and len(elig) >= 2:
            self.decision_log.append({"ts": ts, "chosen": sym, "candidates": [r[0] for r in elig], "entered": None})
        return {"kind": "enter", "symbol": sym, "signal": sig, "window_fn": window_fn(sym), "scale": scale, "extra": self._extras(sym, sig, ai, n, len(elig))}

    def _execute_pending(self, pending: dict, prices: dict[str, float], ts: int) -> None:
        if pending["kind"] == "exit":
            if self.pos is not None and self.pos.symbol == pending["symbol"]:
                self._market_exit(prices[pending["symbol"]], ts, "crossover_exit")
        elif self.pos is None:
            # ATR comes from the closed-candle signal; the stop is placed from the actual execution price
            self._try_enter(pending["symbol"], pending["signal"], prices[pending["symbol"]], ts, pending["window_fn"],
                            risk_scale=pending["scale"], extra=pending["extra"])

    def _snapshot(self, ts: int, mark: float | None) -> None:
        eq = self.equity
        p = self.pos
        if p is not None and mark is not None:
            eq += (mark - p.signal_price) * p.qty * _sign(p.side) - p.funding_paid - p.fee_entry - p.slip_entry
        self.curve.append((ts, eq))


# ------------------------------------------------------------------ driver


def _full_timeline(pairs: dict[str, PairData], use_exec: bool) -> list[int]:
    sets = []
    for p in pairs.values():
        bars = p.exec_bars if use_exec else p.hourly
        sets.append({c.timestamp for c in bars})
    return sorted(set.intersection(*sets))


def timeline_key(timeline: list[int]) -> str:
    """Identifies a timeline for caching a tape built on it."""
    return f"{len(timeline)}:{timeline[0] if timeline else 0}:{timeline[-1] if timeline else 0}"


def run_backtest(
    pairs: dict[str, PairData],
    config: BacktestConfig,
    *,
    tapes: dict[str, SignalTape] | None = None,
    closed_tapes: dict[str, SignalTape] | None = None,
    aux: dict[str, HourlyAux] | None = None,
) -> RunResult:
    """`tapes` are the replay-mode signal tapes (built on the FULL common 5-minute timeline), `closed_tapes`
    the closed-candle ones (one row per hourly candle). Both are optional: when absent they are built here.
    Supplying them only makes repeated runs faster; the result is identical."""
    if not pairs:
        raise BacktestError("no pairs supplied")
    if config.mode not in (MODE_REPLAY, MODE_CLOSED):
        raise BacktestError(f"unknown mode {config.mode!r}")
    if config.auto and len(pairs) < 2:
        raise BacktestError("auto mode needs a universe of at least two pairs")
    if not config.auto and len(pairs) != 1:
        raise BacktestError("fixed-pair mode takes exactly one pair")
    if config.policy.auto_score not in AUTO_SCORES:
        raise BacktestError(f"unknown auto_score {config.policy.auto_score!r}")
    have_exec = all(p.exec_bars for p in pairs.values())
    if config.mode == MODE_REPLAY and not have_exec:
        raise BacktestError("production_replay needs 5-minute data for every pair (it replays the 5-minute polls)")
    sp = dict(
        ema_fast_period=config.strategy.ema_fast_period, ema_slow_period=config.strategy.ema_slow_period, atr_period=config.strategy.atr_period,
        atr_flat_threshold=config.strategy.atr_flat_threshold, atr_rolling_window=config.strategy.atr_rolling_window,
    )
    limit = config.strategy.ohlcv_limit
    warm = config.warmup_bars if config.warmup_bars is not None else max(config.strategy.ema_slow_period + 5, config.strategy.atr_rolling_window + 5)
    if aux is None:
        aux = {s: tapelib.build_hourly_aux(p.hourly, config.strategy.atr_period) for s, p in pairs.items()}
    engine = _Engine(pairs, config, aux)

    use_exec = have_exec
    timeline = _full_timeline(pairs, use_exec)
    if not timeline:
        raise BacktestError("the pairs share no " + ("5-minute" if use_exec else "hourly") + " timestamps")
    k0 = bisect.bisect_left(timeline, config.start_ts) if config.start_ts is not None else 0
    k1 = bisect.bisect_left(timeline, config.end_ts) if config.end_ts is not None else len(timeline)
    if k0 >= k1:
        raise BacktestError("the requested window contains no data")

    if config.mode == MODE_REPLAY:
        if tapes is None:
            tapes = {s: tapelib.build_replay_tape(s, p.hourly, timeline, p.exec_bars, sp, limit=limit, warm=warm) for s, p in pairs.items()}
        for s, t in tapes.items():
            if len(t) != len(timeline):
                raise BacktestError(f"the replay tape for {s} was built on a different timeline")
        _walk_replay(engine, timeline, k0, k1, tapes)
        engine.notes.append("production_replay: signals were read from the still-forming hourly candle at 5-minute polls")
    else:
        if closed_tapes is None:
            closed_tapes = {s: tapelib.build_closed_tape(s, p.hourly, sp, limit=limit, warm=warm) for s, p in pairs.items()}
        if have_exec:
            _walk_closed_5m(engine, timeline, k0, k1, closed_tapes)
        else:
            engine.notes.append("hourly-only execution: entries at the next bar's open; intrabar order of events inside a bar is unknown")
            _walk_closed_hourly(engine, timeline, k0, k1, closed_tapes)
    _diagnose_confirmation(engine, closed_tapes, sp, limit, warm)
    return RunResult(
        config=asdict(config), symbols=engine.symbols, start_equity=engine.start_equity, end_equity=engine.equity,
        trades=engine.trades, equity=engine.curve, counters=engine.counters,
        span=(engine.curve[0][0], engine.curve[-1][0]) if engine.curve else (0, 0), notes=engine.notes, decisions=engine.decision_log,
    )


# --------------------------- walkers


def _aligned_bars(pairs: dict[str, PairData], timeline: list[int], use_exec: bool) -> dict[str, list[Candle]]:
    out = {}
    for s, p in pairs.items():
        by_ts = {c.timestamp: c for c in (p.exec_bars if use_exec else p.hourly)}
        out[s] = [by_ts[t] for t in timeline]
    return out


def _walk_replay(e: _Engine, tl: list[int], k0: int, k1: int, tapes: dict[str, SignalTape]) -> None:
    step = 300_000
    limit = e.cfg.strategy.ohlcv_limit
    bars_at = _aligned_bars(e.pairs, tl, True)
    e.curve.append((tl[k0], e.equity))

    def window_for(k: int, hs: int):
        def factory(sym: str):
            def build() -> list[Candle]:
                idx = e.hour_pos[sym][hs]
                j = k
                bars = bars_at[sym]
                while j - 1 >= 0 and tl[j - 1] - tl[j - 1] % HOUR == hs:
                    j -= 1
                chunk = bars[j : k + 1]
                forming = Candle(hs, chunk[0].open, max(c.high for c in chunk), min(c.low for c in chunk), chunk[-1].close, sum(c.volume for c in chunk))
                return e.pairs[sym].hourly[max(0, idx - (limit - 1)) : idx] + [forming]
            return build
        return factory

    for k in range(k0, k1):
        ts = tl[k]
        end = ts + step
        hs = ts - ts % HOUR
        if e.pos is not None:
            b = bars_at[e.pos.symbol][k]
            e._apply_funding(ts, end, b.open)
            if not e._check_stop(b, end):
                e._track_excursion(b)
                e._update_stops(b.close)
        if e.pending is not None:  # a candle-close entry decided at the previous poll, executed at this one
            pend, e.pending = e.pending, None
            e._execute_pending(pend, {s: bars_at[s][k].close for s in e.symbols}, end)
        wanted = [e.pos.symbol] if e.pos is not None else e.symbols
        sigs = {}
        for sym in wanted:
            sg = tapes[sym].signal(k)
            if sg is None:
                sigs = None
                break
            sigs[sym] = sg
        if sigs is not None:
            prices = {s: bars_at[s][k].close for s in wanted}
            aux_idx = {s: _prev_closed_idx(e, s, hs) for s in wanted}
            e._decide_replay(sigs, prices, end, aux_idx, window_for(k, hs), end % HOUR == 0)
        if end % HOUR == 0:
            e._snapshot(end, bars_at[e.pos.symbol][k].close if e.pos is not None else None)
    kl = k1 - 1
    if e.pos is not None:
        e._market_exit(bars_at[e.pos.symbol][kl].close, tl[kl] + step, "end_of_data")


def _prev_closed_idx(e: _Engine, sym: str, hs: int) -> int | None:
    """Hourly index of the last CLOSED hour before the hour starting at `hs` (what a causal filter may use)."""
    i = e.hour_pos[sym].get(hs)
    return None if i is None or i == 0 else i - 1


def _walk_closed_5m(e: _Engine, tl: list[int], k0: int, k1: int, ctapes: dict[str, SignalTape]) -> None:
    step = 300_000
    bars_at = _aligned_bars(e.pairs, tl, True)
    e.curve.append((tl[k0], e.equity))
    pending: dict | None = None
    for k in range(k0, k1):
        ts = tl[k]
        end = ts + step
        prices = {sym: bars_at[sym][k].close for sym in e.symbols}
        if e.pos is not None:
            b = bars_at[e.pos.symbol][k]
            e._apply_funding(ts, end, b.open)
            if not e._check_stop(b, end):
                e._track_excursion(b)
                e._update_stops(b.close)
        # execute what the previous hour-close decided, at this poll
        if pending is not None:
            e._execute_pending(pending, prices, end)
            pending = None
        if end % HOUR == 0:  # an hourly candle just closed: decide on it, execute next poll
            hs = ts - ts % HOUR
            wanted = [e.pos.symbol] if e.pos is not None else e.symbols
            sigs, aux_idx = {}, {}
            for sym in wanted:
                idx = e.hour_pos[sym].get(hs)
                sg = ctapes[sym].signal(idx) if idx is not None else None
                if sg is None:
                    sigs = None
                    break
                sigs[sym], aux_idx[sym] = sg, idx
            if sigs is not None:
                pending = e._decide_closed(sigs, aux_idx, lambda sym, hs=hs: _closed_window_fn(e, sym, hs), end)
            e._snapshot(end, bars_at[e.pos.symbol][k].close if e.pos is not None else None)
    kl = k1 - 1
    if e.pos is not None:
        e._market_exit(bars_at[e.pos.symbol][kl].close, tl[kl] + step, "end_of_data")


def _closed_window_fn(e: _Engine, sym: str, hs: int):
    return lambda: e._closed_window(sym, e.hour_pos[sym][hs])


def _walk_closed_hourly(e: _Engine, tl: list[int], k0: int, k1: int, ctapes: dict[str, SignalTape]) -> None:
    bars_at = _aligned_bars(e.pairs, tl, False)
    e.curve.append((tl[k0], e.equity))
    pending: dict | None = None
    for k in range(k0, k1):
        ts = tl[k]
        end = ts + HOUR
        if pending is not None:
            e._execute_pending(pending, {sym: bars_at[sym][k].open for sym in e.symbols}, ts)
            pending = None
        if e.pos is not None:
            b = bars_at[e.pos.symbol][k]
            e._apply_funding(ts, end, b.open)
            if not e._check_stop(b, end):
                e._track_excursion(b)
                e._update_stops(b.close)
        wanted = [e.pos.symbol] if e.pos is not None else e.symbols
        sigs, aux_idx = {}, {}
        for sym in wanted:
            idx = e.hour_pos[sym].get(ts)
            sg = ctapes[sym].signal(idx) if idx is not None else None
            if sg is None:
                sigs = None
                break
            sigs[sym], aux_idx[sym] = sg, idx
        if sigs is not None:
            pending = e._decide_closed(sigs, aux_idx, lambda sym, ts=ts: _closed_window_fn(e, sym, ts), end)
        e._snapshot(end, bars_at[e.pos.symbol][k].close if e.pos is not None else None)
    kl = k1 - 1
    if e.pos is not None:
        e._market_exit(bars_at[e.pos.symbol][kl].close, tl[kl] + HOUR, "end_of_data")


# ------------------------------------------------------------------ diagnostics


def _diagnose_confirmation(e: _Engine, closed_tapes: dict[str, SignalTape] | None, sp: dict, limit: int, warm: int) -> None:
    """Replay mode only. For each entry, was the signal STILL there once its hour had closed?
    Computed after the run from data the decision could not see; it never influences a trade."""
    if e.cfg.mode != MODE_REPLAY or not e.trades:
        return
    expect = {"bullish_crossover": CODE_BULL, "bearish_crossover": CODE_BEAR}
    tapes = dict(closed_tapes or {})
    for t in e.trades:
        hs = (t.entry_ts - 1) // HOUR * HOUR
        idx = e.hour_pos[t.symbol].get(hs)
        if idx is None:
            continue
        if t.symbol not in tapes:
            p = e.pairs[t.symbol]
            tapes[t.symbol] = tapelib.build_closed_tape(t.symbol, p.hourly, sp, limit=limit, warm=warm)
        code = tapes[t.symbol].code(idx)
        if code != CODE_NA:
            t.signal_confirmed_at_close = code == expect.get(t.entry_signal)
