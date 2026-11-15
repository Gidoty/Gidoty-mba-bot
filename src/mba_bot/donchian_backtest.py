"""Pre-registered backtest runner for donchian_breakout_v1. OFFLINE: it reads candle files the owner downloaded; it makes no network call, uses no
key, places no order and touches no database. docs/research/donchian_preregistration.md is the registration; `plan` prints its hash.

    python -m mba_bot.donchian_backtest plan
    python -m mba_bot.donchian_backtest tune    --root research_data --out research_out/donchian [--expect-plan-hash H]
    python -m mba_bot.donchian_backtest confirm --root research_data --out research_out/donchian --confirm --reason "..."

Same protocol as rsi_backtest: of the last 6 months the first 4 are TUNED on (a 3-day embargo follows) and the last 2 are SEALED and read exactly once
by `confirm`; a second look needs --second-look and the report says it is no longer unseen evidence. The candle files use the research downloader's
layout (<root>/klines/1h/<SYMBOL>.csv with ts,open,high,low,close,volume,...); VOLUME is used (the volume confirmation reads it). Nothing in this file
says the strategy makes money; the numbers come from the owner's run.

Simulation rules (fixed in the registration): the signal is read on a CLOSED candle and the trade enters at the NEXT candle's open; slippage is applied
against the trade on every entry and exit; fees are charged on both notionals; the stop wins when a candle touches the stop and the trailing level
checks come first; one position per pair; a cooldown after an initial-stop exit on the same pair; a trade still open at the end of its window is not
counted (the count is reported). The indicators are computed once over the whole history (they are causal). The live cycle sees at most OHLCV_LIMIT
candles, so its ATR smoothing can differ slightly. Funding on Futures is not modelled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import donchian as DN
from . import rsi_backtest as RB
from .indicators import atr as atr_series
from .rsi_backtest import (CHALLENGER_DD_FACTOR, CHALLENGER_MARGIN_R, EMBARGO_DAYS, HOUR_MS, MARKETS, MIN_TRADES, PAIRS, SEALED_MONTHS, SLIPPAGE_BPS,
                           TOTAL_MONTHS, SealedError, SimTrade, data_hash, load_all, metrics, windows)
from .strategy import Candle

PROTOCOL = "donchian_breakout_v1 backtest, 4 months tuned + 2 months sealed, v1"
COOLDOWN_HOURS = 4.0  # after an initial-stop exit on a pair (the live default, DONCHIAN_STOP_COOLDOWN_HOURS)


@dataclass(frozen=True)
class Arm:
    name: str
    params: DN.DonchianParams
    note: str


ARMS = (
    Arm("SPEC", DN.DonchianParams(), "the specification: 20-candle entry channel, 10-candle exit channel, volume 1.5x, ATR% limit 3x, stop min(mid, 1.5 ATR), trail 2 ATR"),
    Arm("VOL1.2", DN.DonchianParams(volume_mult=1.2), "looser volume confirmation"),
    Arm("VOL2.0", DN.DonchianParams(volume_mult=2.0), "stricter volume confirmation"),
    Arm("ENTRY30", DN.DonchianParams(entry_period=30, volume_period=30), "longer entry channel"),
    Arm("TRAIL3.0", DN.DonchianParams(trail_atr=3.0), "looser trailing stop"),
)


def plan_hash() -> str:
    blob = {"protocol": PROTOCOL, "arms": [{"name": a.name, "params": asdict(a.params)} for a in ARMS], "pairs": PAIRS, "markets": MARKETS,
            "slippage_bps": SLIPPAGE_BPS, "months": [TOTAL_MONTHS, SEALED_MONTHS, EMBARGO_DAYS], "risk_pct": RB.RISK_PCT, "cooldown_hours": COOLDOWN_HOURS,
            "rule": [MIN_TRADES, CHALLENGER_MARGIN_R, CHALLENGER_DD_FACTOR], "strategy_sha256": RB._sha(Path(DN.__file__)), "runner_rules": "next-open entry, stop first, volume used"}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]


def simulate(symbol: str, candles: list[Candle], p: DN.DonchianParams, market: str, start_ms: int, end_ms: int, cooldown_hours: float = COOLDOWN_HOURS) -> tuple[list[SimTrade], int]:
    """Trades whose signal candle and exit both lie inside [start_ms, end_ms). Returns (trades, unfinished_count)."""
    atr_values = atr_series([c.high for c in candles], [c.low for c in candles], [c.close for c in candles], p.atr_period)
    allow_short = market == "futures"
    fee = MARKETS[market] / 1e4
    slip = SLIPPAGE_BPS / 1e4
    cooldown_ms = int(cooldown_hours * 3_600_000)
    trades: list[SimTrade] = []
    unfinished = 0
    cooldown_until = 0
    i = DN.min_candles(p) - 1
    n = len(candles)
    while i < n - 1:
        c = candles[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + HOUR_MS > end_ms:
            break
        if c.timestamp + HOUR_MS < cooldown_until:
            i += 1
            continue
        sig = DN.signal_at(candles, atr_values, i, p, allow_short=allow_short)
        if sig.side is None:
            i += 1
            continue
        nxt = candles[i + 1]
        entry = nxt.open * (1 + slip) if sig.side == "long" else nxt.open * (1 - slip)
        stop = DN.initial_stop(entry, sig.side, sig.mid, sig.atr, p)
        if not ((sig.side == "long" and stop < entry) or (sig.side == "short" and stop > entry)):
            i += 1  # the live entry refuses a stop on the wrong side; so does the simulation
            continue
        ex = DN.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=candles, tf_ms=HOUR_MS, p=p, atr_values=atr_values)
        if ex is None or ex.candle_ts + HOUR_MS > end_ms:
            unfinished += 1
            i += 1
            continue
        exit_price = ex.price * (1 - slip) if sig.side == "long" else ex.price * (1 + slip)
        dist = abs(entry - stop)
        gross_move = (exit_price - entry) if sig.side == "long" else (entry - exit_price)
        fees_money = fee * (entry + exit_price)
        trades.append(SimTrade(symbol, sig.side, nxt.timestamp, ex.candle_ts, entry, exit_price, stop, ex.reason, gross_move / dist,
                               (gross_move - fees_money) / dist, (gross_move - fees_money) / entry))
        if ex.reason == DN.EXIT_STOP:
            cooldown_until = ex.candle_ts + HOUR_MS + cooldown_ms
        j = i + 1
        while j < n and candles[j].timestamp < ex.candle_ts:
            j += 1
        i = max(j, i + 1)
    return trades, unfinished


def run_arm(candles_by_pair: dict[str, list[Candle]], arm: Arm, start_ms: int, end_ms: int) -> dict:
    out: dict = {}
    for market in MARKETS:
        per, pooled, unfinished = {}, [], 0
        for sym, cs in candles_by_pair.items():
            tr, u = simulate(sym, cs, arm.params, market, start_ms, end_ms)
            per[sym] = metrics(tr, start_ms, end_ms)
            pooled += tr
            unfinished += u
        per["ALL"] = metrics(pooled, start_ms, end_ms)
        per["unfinished"] = unfinished
        per["by_reason"] = {k: sum(1 for x in pooled if x.reason == k) for k in sorted({x.reason for x in pooled})}
        out[market] = per
    return out


def run_tune(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise ValueError(f"the plan hash {ph} differs from the registered {expect_plan_hash}; nothing was run")
    data = load_all(root)
    w = windows(data)
    t0, t1 = w["tune"]
    rows = {arm.name: run_arm(data, arm, t0, t1) for arm in ARMS}
    chosen = RB.select(rows)
    result = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": data_hash(root), "created": RB._stamp(), "windows": {k: list(v) for k, v in w.items()}, "rows": rows,
              "selected": chosen, "sealed_months_read": False}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tune_results.json").write_text(json.dumps(result, indent=1))
    (out_dir / "selected.json").write_text(json.dumps({k: result[k] for k in ("protocol", "plan_hash", "data_hash", "windows", "selected")}, indent=1))
    (out_dir / "tune_report.md").write_text(tune_report(result))
    return result


def tune_report(res: dict) -> str:
    t0, t1 = res["windows"]["tune"]
    lines = [f"# donchian_breakout_v1 backtest: tuning window {RB._date(t0)} to {RB._date(t1)} (sealed months NOT read)", "",
             f"Plan hash {res['plan_hash']}, data hash {res['data_hash']}. This is the window parameters were compared on. It is NOT evidence that the strategy works.", "",
             "Expectancy is per trade after fees and slippage. R is the money at risk on the trade (entry to stop).", ""]
    for arm, row in res["rows"].items():
        lines.append(f"## {arm}")
        for market in MARKETS:
            lines.append(f"* {market}, all pairs pooled (no slot limit): {RB._fmt(row[market]['ALL'])}; unfinished at window end: {row[market]['unfinished']}; exits: {row[market]['by_reason']}")
    lines += ["", "## Selected under the registered rule", f"* {res['selected']['arm']}: {res['selected']['why']}", "",
              "Nothing above says the strategy is profitable. The sealed months are read once, by `confirm`."]
    return "\n".join(lines) + "\n"


def run_confirm(root: Path, out_dir: Path, *, reason: str, confirm: bool, second_look: bool = False) -> dict:
    out_dir = Path(out_dir)
    sel = out_dir / "selected.json"
    if not sel.exists():
        raise SealedError("run the tuning first: there is no selected.json")
    if not confirm:
        raise SealedError("opening the sealed months needs an explicit --confirm")
    if not (reason or "").strip():
        raise SealedError("opening the sealed months needs a reason")
    chosen = json.loads(sel.read_text())
    ph, dh = plan_hash(), data_hash(root)
    if chosen["plan_hash"] != ph:
        raise SealedError(f"plan_hash differs from the tuning ({chosen['plan_hash']} against {ph}); the strategy or the plan changed after tuning")
    if chosen["data_hash"] != dh:
        raise SealedError("data_hash differs from the tuning: the candle files changed")
    data = load_all(root)
    w = windows(data)
    if list(w["sealed"]) != chosen["windows"]["sealed"]:
        raise SealedError("the sealed window changed since the tuning")
    ledger = out_dir / "looks.json"
    looks = json.loads(ledger.read_text()) if ledger.exists() else []
    contaminated = bool(looks)
    if looks and not second_look:
        raise SealedError("the sealed months were already read once; a second look needs --second-look and is labelled contaminated")
    looks.append({"ts": RB._stamp(), "reason": reason.strip(), "plan_hash": ph, "data_hash": dh, "second_look": contaminated})
    ledger.write_text(json.dumps(looks, indent=1))  # recorded BEFORE anything is read
    s0, s1 = w["sealed"]
    arms = {a.name: a for a in ARMS}
    names = ["SPEC"] if chosen["selected"]["arm"] == "SPEC" else ["SPEC", chosen["selected"]["arm"]]
    rows = {n: run_arm(data, arms[n], s0, s1) for n in names}
    result = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": dh, "created": RB._stamp(), "sealed_window": [s0, s1], "selected": chosen["selected"], "rows": rows,
              "contaminated": contaminated, "looks": len(looks)}
    (out_dir / "sealed_results.json").write_text(json.dumps(result, indent=1))
    (out_dir / "final_report.md").write_text(final_report(result))
    return result


def final_report(res: dict) -> str:
    s0, s1 = res["sealed_window"]
    lines = [f"# donchian_breakout_v1 backtest: final report, sealed window {RB._date(s0)} to {RB._date(s1)}", "",
             f"Plan hash {res['plan_hash']}, data hash {res['data_hash']}. These two months were read ONCE after the parameters were fixed on the earlier four.", ""]
    if res["contaminated"]:
        lines += ["**WARNING: this was NOT the first look at the sealed months. The result is no longer unseen evidence.**", ""]
    negative = []
    for arm, row in res["rows"].items():
        lines.append(f"## {arm}" + (" (selected on the tuning window)" if arm == res["selected"]["arm"] else " (the specification)"))
        for market in MARKETS:
            lines.append(f"### {market}")
            lines.append(f"* All pairs pooled (no slot limit, an upper bound on frequency): {RB._fmt(row[market]['ALL'])}")
            for sym in PAIRS:
                lines.append(f"* {sym}: {RB._fmt(row[market][sym])}")
            lines.append(f"* Exits: {row[market]['by_reason']}; unfinished at window end (not counted): {row[market]['unfinished']}")
            e = row[market]["ALL"]["expectancy_r"]
            if e is not None and e < 0:
                negative.append(f"{arm} on {market}")
        lines.append("")
    if negative:
        lines += ["## Result", "**The untouched two months are NEGATIVE after fees and slippage for: " + "; ".join(negative) + ". The strategy lost money there.**", ""]
    else:
        lines += ["## Result", "Expectancy after fees and slippage was not negative on the untouched months for the arms shown. Two months is short: this can be luck, "
                  "and it says nothing about other regimes. Breakout strategies often have a low win rate and a few large winners; two months may hold none.", ""]
    lines += ["## Combined trades per week for all three strategies", "NOT MEASURED. This runner measures donchian_breakout_v1 only; rsi_backtest measures rsi_mr_v1 only; "
              "ema_cross_atr_v1 is measured by the research branch engine. The three figures use different runners and cannot be added into a measured combined figure. "
              "The pooled donchian_breakout_v1 figure above is the only frequency measured here.", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.donchian_backtest", description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["plan", "tune", "confirm"])
    ap.add_argument("--root", default="research_data")
    ap.add_argument("--out", default="research_out/donchian")
    ap.add_argument("--expect-plan-hash")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--second-look", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.action == "plan":
            print(json.dumps({"plan_hash": plan_hash(), "arms": [x.name for x in ARMS], "pairs": PAIRS, "markets": MARKETS, "slippage_bps": SLIPPAGE_BPS,
                              "cooldown_hours": COOLDOWN_HOURS, "months": [TOTAL_MONTHS, SEALED_MONTHS, EMBARGO_DAYS]}, indent=1))
            return 0
        if a.action == "tune":
            run_tune(Path(a.root), Path(a.out), expect_plan_hash=a.expect_plan_hash)
            print((Path(a.out) / "tune_report.md").read_text())
            return 0
        run_confirm(Path(a.root), Path(a.out), reason=a.reason, confirm=a.confirm, second_look=a.second_look)
        print((Path(a.out) / "final_report.md").read_text())
        return 0
    except (SealedError, ValueError, FileNotFoundError) as exc:
        print(f"donchian_backtest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
