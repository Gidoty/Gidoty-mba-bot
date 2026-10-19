"""Validation methodology: Fixed/Manual Pair vs. Auto Select, over
historical candle data, without look-ahead bias.

This is NOT a Mainnet-readiness certification by itself. This sandbox has
no live Binance network access (outbound requests to api.binance.com are
blocked here - confirmed directly, not assumed), so every run of this
module to date has used synthetic or externally-supplied OHLCV, never
genuine historical Binance data for the approved universe. Per the
capital-control implementation approval: Auto Select must be treated as
unvalidated for Mainnet recommendation until this module has actually been
run against real historical data and the results reviewed.

No look-ahead: at bar index `i`, every decision (entry, exit, and Auto
Select's ranking) only ever sees `candles[: i + 1]` - never a later bar.
Both walks reuse the exact same production functions `trader.py` itself
calls (`strategy.generate_signal`, `risk.compute_stop_loss`,
`risk.compute_position_size`, `pair_selection.rank_candidates`) rather
than a second, parallel implementation that could silently drift from
what actually trades live.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pair_selection import PairCandidate, rank_candidates
from .risk import compute_position_size, compute_stop_loss
from .strategy import Candle, SignalType, generate_signal


@dataclass(frozen=True)
class BacktestTrade:
    pair: str
    side: str
    entry_index: int
    entry_price: float
    exit_index: int
    exit_price: float
    pnl: float


@dataclass(frozen=True)
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    starting_balance: float = 0.0
    ending_balance: float = 0.0

    @property
    def total_return_pct(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return (self.ending_balance - self.starting_balance) / self.starting_balance * 100

    @property
    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades)

    @property
    def max_drawdown_pct(self) -> float:
        """Largest peak-to-trough drop in the equity curve, as a
        percentage of the peak at the time - the standard measure of
        "how bad did it get" that a total-return figure alone hides."""
        balance = self.starting_balance
        peak = balance
        worst = 0.0
        for trade in self.trades:
            balance += trade.pnl
            peak = max(peak, balance)
            if peak > 0:
                worst = max(worst, (peak - balance) / peak * 100)
        return worst


def _entry_side(signal_type: SignalType, *, market_type: str) -> str | None:
    """Exactly trader.py's `_maybe_enter` entry-side rule - Spot is
    long-only, Futures may also short. Kept as a tiny shared helper so
    this module can never drift from that rule independently."""
    if signal_type == SignalType.BULLISH_CROSSOVER:
        return "long"
    if signal_type == SignalType.BEARISH_CROSSOVER and market_type == "futures":
        return "short"
    return None


def _should_exit(open_side: str, signal_type: SignalType, *, current_price: float, stop_price: float) -> tuple[bool, float | None]:
    """Exactly trader.py's `_maybe_exit` exit rule. Returns (should_exit, exit_price)."""
    hit_stop = (open_side == "long" and current_price <= stop_price) or (open_side == "short" and current_price >= stop_price)
    opposite_cross = (open_side == "long" and signal_type == SignalType.BEARISH_CROSSOVER) or (
        open_side == "short" and signal_type == SignalType.BULLISH_CROSSOVER
    )
    if not (hit_stop or opposite_cross):
        return False, None
    return True, (stop_price if hit_stop else current_price)


def _pnl(open_side: str, entry_price: float, exit_price: float, quantity: float) -> float:
    if open_side == "long":
        return (exit_price - entry_price) * quantity
    return (entry_price - exit_price) * quantity


def run_fixed_pair_backtest(
    pair: str,
    candles: list[Candle],
    *,
    market_type: str,
    starting_balance: float,
    risk_pct: float,
    max_risk_pct: float,
    atr_stop_multiplier: float,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
    atr_flat_threshold: float,
    atr_rolling_window: int,
) -> BacktestResult:
    """Fixed/Manual Pair mode's backtest: one pair, one open position at
    a time, the exact same entry/exit rules as `trader.py`. Walks
    forward bar by bar; at bar `i`, `generate_signal` only ever sees
    `candles[: i + 1]`."""
    balance = starting_balance
    trades: list[BacktestTrade] = []
    open_trade: dict | None = None

    for i in range(len(candles)):
        window = candles[: i + 1]
        current_price = window[-1].close
        signal = generate_signal(
            window,
            ema_fast_period=ema_fast_period,
            ema_slow_period=ema_slow_period,
            atr_period=atr_period,
            atr_flat_threshold=atr_flat_threshold,
            atr_rolling_window=atr_rolling_window,
        )

        if open_trade is None:
            side = _entry_side(signal.signal, market_type=market_type)
            if side is None or not signal.atr:
                continue
            stop_price = compute_stop_loss(
                entry_price=current_price, atr=signal.atr, side=side, atr_multiplier=atr_stop_multiplier
            )
            size = compute_position_size(
                account_balance=balance,
                risk_pct=risk_pct,
                entry_price=current_price,
                stop_price=stop_price,
                max_risk_pct=max_risk_pct,
            )
            if size.quantity <= 0:
                continue
            open_trade = {
                "side": side,
                "entry_price": current_price,
                "stop_price": stop_price,
                "quantity": size.quantity,
                "entry_index": i,
            }
        else:
            should_exit, exit_price = _should_exit(
                open_trade["side"], signal.signal, current_price=current_price, stop_price=open_trade["stop_price"]
            )
            if not should_exit:
                continue
            pnl = _pnl(open_trade["side"], open_trade["entry_price"], exit_price, open_trade["quantity"])
            balance += pnl
            trades.append(
                BacktestTrade(
                    pair=pair,
                    side=open_trade["side"],
                    entry_index=open_trade["entry_index"],
                    entry_price=open_trade["entry_price"],
                    exit_index=i,
                    exit_price=exit_price,
                    pnl=pnl,
                )
            )
            open_trade = None

    return BacktestResult(trades=trades, starting_balance=starting_balance, ending_balance=balance)


def run_auto_select_backtest(
    candles_by_pair: dict[str, list[Candle]],
    *,
    market_type: str,
    starting_balance: float,
    risk_pct: float,
    max_risk_pct: float,
    atr_stop_multiplier: float,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
    atr_flat_threshold: float,
    atr_rolling_window: int,
) -> BacktestResult:
    """Auto Select's backtest: same walk-forward discipline, but when
    flat, ranks every pair in `candles_by_pair` using
    `pair_selection.rank_candidates` - the exact production ranking
    function - with each candidate's candles truncated to
    `candles[: i + 1]` first, so ranking at bar `i` can never see bar
    `i + 1` or later. Never switches pairs while a position is open -
    the same invariant `trader.py`'s scheduler-level guard enforces
    live (this loop simply never runs the scan while `open_trade` is
    set, mirroring that code path exactly).

    Assumes every pair's candle list is the same length and aligned to
    the same timestamps (bar `i` is the same point in time for every
    pair) - a reasonable simplifying assumption for a historical
    comparison tool, not something trader.py's live code path needs to
    assume."""
    length = len(next(iter(candles_by_pair.values()), []))
    balance = starting_balance
    trades: list[BacktestTrade] = []
    open_trade: dict | None = None

    for i in range(length):
        if open_trade is None:
            candidates = [
                PairCandidate(pair=pair, candles=candles[: i + 1]) for pair, candles in candles_by_pair.items()
            ]
            ranked = rank_candidates(
                candidates,
                market_type=market_type,
                ema_fast_period=ema_fast_period,
                ema_slow_period=ema_slow_period,
                atr_period=atr_period,
                atr_flat_threshold=atr_flat_threshold,
                atr_rolling_window=atr_rolling_window,
            )
            if not ranked:
                continue
            best_pair = ranked[0].pair
            window = candles_by_pair[best_pair][: i + 1]
            current_price = window[-1].close
            signal = generate_signal(
                window,
                ema_fast_period=ema_fast_period,
                ema_slow_period=ema_slow_period,
                atr_period=atr_period,
                atr_flat_threshold=atr_flat_threshold,
                atr_rolling_window=atr_rolling_window,
            )
            side = _entry_side(signal.signal, market_type=market_type)
            if side is None or not signal.atr:
                continue
            stop_price = compute_stop_loss(
                entry_price=current_price, atr=signal.atr, side=side, atr_multiplier=atr_stop_multiplier
            )
            size = compute_position_size(
                account_balance=balance,
                risk_pct=risk_pct,
                entry_price=current_price,
                stop_price=stop_price,
                max_risk_pct=max_risk_pct,
            )
            if size.quantity <= 0:
                continue
            open_trade = {
                "pair": best_pair,
                "side": side,
                "entry_price": current_price,
                "stop_price": stop_price,
                "quantity": size.quantity,
                "entry_index": i,
            }
        else:
            window = candles_by_pair[open_trade["pair"]][: i + 1]
            current_price = window[-1].close
            signal = generate_signal(
                window,
                ema_fast_period=ema_fast_period,
                ema_slow_period=ema_slow_period,
                atr_period=atr_period,
                atr_flat_threshold=atr_flat_threshold,
                atr_rolling_window=atr_rolling_window,
            )
            should_exit, exit_price = _should_exit(
                open_trade["side"], signal.signal, current_price=current_price, stop_price=open_trade["stop_price"]
            )
            if not should_exit:
                continue
            pnl = _pnl(open_trade["side"], open_trade["entry_price"], exit_price, open_trade["quantity"])
            balance += pnl
            trades.append(
                BacktestTrade(
                    pair=open_trade["pair"],
                    side=open_trade["side"],
                    entry_index=open_trade["entry_index"],
                    entry_price=open_trade["entry_price"],
                    exit_index=i,
                    exit_price=exit_price,
                    pnl=pnl,
                )
            )
            open_trade = None

    return BacktestResult(trades=trades, starting_balance=starting_balance, ending_balance=balance)
