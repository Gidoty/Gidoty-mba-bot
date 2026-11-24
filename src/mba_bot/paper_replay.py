"""B0, the paper replay (read-only, for the owner to run on files): compare each PAPER trade with what mainnet candles say.

    python -m mba_bot.paper_replay --trades paper_trades.csv --klines research_data/klines/1h [--csv replay.csv]

Inputs: the CSV from `python -m mba_bot.paper_export` (or docs/ops/paper_trades_export.sql) and mainnet hourly candles from the research downloader (`python -m mba_bot.research download`), which stores
USD-M Futures candles as `<dir>/<SYMBOL>.csv` with the columns ts,open,high,low,close,volume,... (`ts` = bar open time, epoch milliseconds, UTC). It reads those two kinds of file and writes nothing except the
optional --csv (never over an existing file). No network, no database, no key.

For each paper trade it reports, on MAINNET candles:
  * whether the paper entry price lies inside the mainnet range of the hour it was opened in;
  * the mainnet EMA signal on the candles that had CLOSED before the entry (does a closed-candle crossover in the trade's direction exist there);
  * whether the stop level was touched on mainnet from the next candle on, and when; whether the paper exit looks consistent with that;
  * the trade's result in R (loss at the stop = -1) on paper and on a mainnet replay: exit at the stop level (or the candle open when it gaps through) if mainnet touched the stop before the paper close,
    otherwise at the close of the hour the paper trade closed.
Then a summary: wins and losses on paper and in the replay, how many agree, the average R of each.

LIMITS (printed with the result): hourly candles only, so the order of events inside an hour is unknown; Spot paper trades are compared with USD-M Futures candles (a close proxy); fees, funding and slippage
are ignored; the signal check uses the EMA parameters given by the flags (defaults = .env.example); a trade whose pair has no candle file or too little history is reported as not comparable."""
from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ema_exits
from .indicators import atr as atr_series
from .strategy import Candle, SignalType, generate_signal

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class Params:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    atr_flat_threshold: float = 0.75
    atr_rolling_window: int = 50
    rules: ema_exits.ExitRules = ema_exits.ExitRules()  # optional: replay the EMA exit rules (take profit, trailing stop, break-even) next to the existing replay; all off = nothing added


def load_candles(path: Path) -> list[Candle]:
    out: list[Candle] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(Candle(timestamp=int(row["ts"]), open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]), volume=float(row.get("volume") or 0.0)))
    out.sort(key=lambda c: c.timestamp)
    return out


def _ms(text: str) -> int | None:
    if not text:
        return None
    dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _index_of(candles: list[Candle], ts_ms: int) -> int | None:
    """Index of the candle containing ts_ms (open <= ts < open + 1h), or None."""
    lo, hi = 0, len(candles) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        c = candles[mid]
        if ts_ms < c.timestamp:
            hi = mid - 1
        elif ts_ms >= c.timestamp + HOUR_MS:
            lo = mid + 1
        else:
            return mid
    return None


def r_multiple(side: str, entry: float, stop: float, exit_price: float) -> float | None:
    risk = abs(entry - stop)
    if not risk or not all(map(math.isfinite, (entry, stop, exit_price))):
        return None
    return ((exit_price - entry) if side == "long" else (entry - exit_price)) / risk


def stop_touch(candles: list[Candle], start: int, end: int, side: str, stop: float):
    """First candle in [start, end] whose range touches the stop: (index, fill price). A candle that OPENS beyond the stop fills at its open (the worse price)."""
    for i in range(start, end + 1):
        c = candles[i]
        if side == "long" and c.low <= stop:
            return i, min(stop, c.open)
        if side == "short" and c.high >= stop:
            return i, max(stop, c.open)
    return None


def replay_trade(trade: dict, candles: list[Candle] | None, params: Params) -> dict:
    out = {"trade_ref": trade["trade_ref"], "pair": trade["pair"], "side": trade["side"], "status": trade["status"], "paper_r": None, "mainnet_r": None, "comparable": False, "note": ""}
    side = trade["side"]
    try:
        entry, stop = float(trade["entry_price"]), float(trade["stop_loss_price"])
    except (TypeError, ValueError):
        out["note"] = "unreadable prices"
        return out
    if trade.get("exit_price") not in ("", None) and trade["status"] == "closed":
        out["paper_r"] = r_multiple(side, entry, stop, float(trade["exit_price"]))
    out["paper_win"] = None if out["paper_r"] is None else out["paper_r"] > 0
    opened, closed = _ms(trade.get("opened_at", "")), _ms(trade.get("closed_at", ""))
    if not candles or opened is None:
        out["note"] = "no candle file for this pair" if not candles else "no open time"
        return out
    i0 = _index_of(candles, opened)
    if i0 is None:
        out["note"] = "the open time is outside the candle file"
        return out
    c0 = candles[i0]
    out["entry_inside_mainnet_hour"] = c0.low <= entry <= c0.high
    out["entry_vs_mainnet_open_pct"] = (entry - c0.open) / c0.open * 100.0 if c0.open else None
    history = [c for c in candles[:i0]]  # only candles that had CLOSED before the entry hour began
    need = params.ema_slow + params.atr_rolling_window + 5
    if len(history) >= need:
        sig = generate_signal(history[-300:], ema_fast_period=params.ema_fast, ema_slow_period=params.ema_slow, atr_period=params.atr_period, atr_flat_threshold=params.atr_flat_threshold,
                              atr_rolling_window=params.atr_rolling_window).signal
        want = SignalType.BULLISH_CROSSOVER if side == "long" else SignalType.BEARISH_CROSSOVER
        out["mainnet_closed_signal"] = sig.value
        out["mainnet_signal_matches_side"] = sig == want
    else:
        out["mainnet_closed_signal"] = "insufficient history"
        out["mainnet_signal_matches_side"] = None
    out["entry_candle_already_touched_stop"] = c0.low <= stop if side == "long" else c0.high >= stop
    if closed is None:
        out["note"] = "trade not closed: paper result unknown"
        return out
    i1 = _index_of(candles, closed)
    if i1 is None or i1 < i0:
        out["note"] = "the close time is outside the candle file"
        return out
    touch = stop_touch(candles, i0 + 1, i1, side, stop) if i1 > i0 else None
    if touch is not None:
        exit_price, hit_at = touch[1], candles[touch[0]].timestamp
        out["mainnet_stop_touched_at"] = datetime.fromtimestamp(hit_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    else:
        exit_price = candles[i1].close
        out["mainnet_stop_touched_at"] = ""
    out["mainnet_r"] = r_multiple(side, entry, stop, exit_price)
    out["mainnet_win"] = None if out["mainnet_r"] is None else out["mainnet_r"] > 0
    reason = trade.get("close_reason", "")
    out["paper_exit_consistent_with_mainnet_stop"] = (touch is not None) == (reason in ("stop_loss", "exchange_stop_triggered", "stop_limit_gap")) if reason else None
    out["comparable"] = out["paper_r"] is not None and out["mainnet_r"] is not None
    if params.rules.any_on and i1 > i0:
        # What the EMA exit rules would have done on the same candles: walk the hours after the entry up to the paper close; if no rule fired, the trade ends where the existing replay ends (the close of the paper-close hour).
        atrs = atr_series([c.high for c in candles[: i1 + 1]], [c.low for c in candles[: i1 + 1]], [c.close for c in candles[: i1 + 1]], params.atr_period)
        w = ema_exits.walk(side, entry, stop, candles[i0 + 1].timestamp, candles[i0 + 1 : i1 + 1], params.rules, atrs[i0 + 1 : i1 + 1])
        out["rules_exit"] = w.exit_reason or "paper_exit_hour_close"
        out["rules_r"] = r_multiple(side, entry, stop, w.exit_price if w.exit_reason else candles[i1].close)
    return out


def summarize(results: list[dict]) -> list[str]:
    comp = [r for r in results if r["comparable"]]
    paper_wins = sum(1 for r in comp if r["paper_win"])
    main_wins = sum(1 for r in comp if r.get("mainnet_win"))
    agree = sum(1 for r in comp if r["paper_win"] == r.get("mainnet_win"))
    allw = sum(1 for r in results if r.get("paper_win"))
    allc = sum(1 for r in results if r.get("paper_win") is not None)
    mean = lambda xs: "n/a" if not xs else f"{sum(xs) / len(xs):+.3f}"
    lines = [
        f"paper trades read: {len(results)}; with a paper result: {allc} ({allw} wins); comparable with mainnet candles: {len(comp)}",
        f"on the comparable trades: paper wins {paper_wins}, mainnet-replay wins {main_wins}, same outcome on both {agree} of {len(comp)}",
        f"average R, paper {mean([r['paper_r'] for r in comp])}, mainnet replay {mean([r['mainnet_r'] for r in comp])}",
    ]
    signalled = [r for r in comp if r.get("mainnet_signal_matches_side") is not None]
    if signalled:
        lines.append(f"mainnet closed-candle signal matched the trade's side at entry: {sum(1 for r in signalled if r['mainnet_signal_matches_side'])} of {len(signalled)}")
    inside = [r for r in comp if r.get("entry_inside_mainnet_hour") is not None]
    if inside:
        lines.append(f"paper entry price inside the mainnet range of its hour: {sum(1 for r in inside if r['entry_inside_mainnet_hour'])} of {len(inside)}")
    with_rules = [r for r in comp if r.get("rules_r") is not None]
    if with_rules:
        by_reason: dict[str, int] = {}
        for r in with_rules:
            by_reason[r["rules_exit"]] = by_reason.get(r["rules_exit"], 0) + 1
        better = sum(1 for r in with_rules if r["rules_r"] > r["mainnet_r"] + 1e-9)
        worse = sum(1 for r in with_rules if r["rules_r"] < r["mainnet_r"] - 1e-9)
        lines.append(f"WITH THE EXIT RULES on the same {len(with_rules)} trades: average R {mean([r['rules_r'] for r in with_rules])} against {mean([r['mainnet_r'] for r in with_rules])} without; "
                     f"better on {better}, worse on {worse}, the same on {len(with_rules) - better - worse}; exits by reason: " + ", ".join(f"{k} {v}" for k, v in sorted(by_reason.items())))
        lines.append("This shows what the rules do to THESE trades. It is a mechanics check, not evidence that the rules improve results: the rules were not chosen on these trades and no claim is made.")
    notes = sorted({r["note"] for r in results if r["note"]})
    if notes:
        lines.append("not comparable because: " + "; ".join(notes))
    lines += ["", "LIMITS: hourly candles (the order of events inside an hour is unknown), Spot paper trades are compared with USD-M Futures candles, fees, funding and slippage are ignored,",
              "and a replay is one reading of the data, not a backtest: it does not re-run the strategy's exits or guards."]
    return lines


def read_trades(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def run(trades_path: Path, klines_dir: Path, params: Params) -> tuple[list[dict], list[str]]:
    trades = read_trades(trades_path)
    cache: dict[str, list[Candle] | None] = {}
    results = []
    for t in trades:
        symbol = (t.get("pair") or "").replace("/", "").upper()
        if symbol not in cache:
            f = klines_dir / f"{symbol}.csv"
            cache[symbol] = load_candles(f) if f.exists() else None
        results.append(replay_trade(t, cache[symbol], params))
    return results, summarize(results)


FIELDS = ("trade_ref", "pair", "side", "status", "paper_r", "mainnet_r", "rules_r", "rules_exit", "paper_win", "mainnet_win", "comparable", "entry_inside_mainnet_hour", "entry_vs_mainnet_open_pct", "mainnet_closed_signal",
          "mainnet_signal_matches_side", "entry_candle_already_touched_stop", "mainnet_stop_touched_at", "paper_exit_consistent_with_mainnet_stop", "note")


def to_csv(results: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in results:
        w.writerow({k: ("" if r.get(k) is None else (f"{r[k]:.4f}" if isinstance(r.get(k), float) else r.get(k))) for k in FIELDS})
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.paper_replay", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", required=True, type=Path)
    ap.add_argument("--klines", required=True, type=Path, help="directory with <SYMBOL>.csv hourly candles, e.g. research_data/klines/1h")
    ap.add_argument("--csv", type=Path, help="also write the per-trade rows here (never overwrites)")
    ap.add_argument("--ema-fast", type=int, default=20)
    ap.add_argument("--ema-slow", type=int, default=50)
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--atr-flat-threshold", type=float, default=0.75)
    ap.add_argument("--atr-rolling-window", type=int, default=50)
    ap.add_argument("--take-profit-r", type=float, default=0.0, help="also replay the EMA take-profit rule (multiples of the initial stop distance); 0 = off")
    ap.add_argument("--trail-trigger-r", type=float, default=0.0, help="also replay the trailing stop once the trade is up this many R; 0 = off")
    ap.add_argument("--trail-atr", type=float, default=2.0, help="trail distance in ATR")
    ap.add_argument("--breakeven-r", type=float, default=0.0, help="also replay the break-even move after this many R; 0 = off")
    args = ap.parse_args(argv)
    try:
        rules = ema_exits.ExitRules(max(0.0, args.take_profit_r), max(0.0, args.trail_trigger_r), args.trail_atr, max(0.0, args.breakeven_r))
        results, summary = run(args.trades, args.klines, Params(args.ema_fast, args.ema_slow, args.atr_period, args.atr_flat_threshold, args.atr_rolling_window, rules))
    except Exception as exc:  # noqa: BLE001 - class name only
        print(f"paper replay: cannot read the inputs ({type(exc).__name__})", file=sys.stderr)
        return 2
    for line in summary:
        print(line)
    if args.csv:
        with open(args.csv, "x", newline="") as fh:
            fh.write(to_csv(results))
        print(f"per-trade rows written to {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
