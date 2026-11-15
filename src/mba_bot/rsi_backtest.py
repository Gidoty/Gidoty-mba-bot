"""Pre-registered backtest runner for rsi_mr_v1. OFFLINE: it reads candle files the owner downloaded; it makes no network call, uses no
key, places no order and touches no database. docs/research/rsi_mr_preregistration.md is the registration; `plan` prints its hash.

    python -m mba_bot.rsi_backtest plan
    python -m mba_bot.rsi_backtest tune    --root research_data --out research_out/rsi_mr [--expect-plan-hash H]
    python -m mba_bot.rsi_backtest confirm --root research_data --out research_out/rsi_mr --confirm --reason "..."

The last 6 months of the data are split: the first 4 months are TUNED on (a 3-day embargo follows); the last 2 months are SEALED and are read
exactly once by `confirm`. A second look needs --second-look and the report says it is no longer unseen evidence. The ledger of looks is a file BESIDE THE DATA keyed on the data hash
(an honesty aid, not a lock).
The candle files use the layout the research downloader writes: <root>/klines/1h/<SYMBOL>.csv with the header
ts,open,high,low,close,volume,... (ts in milliseconds). Nothing in this file says the strategy makes money; the numbers come from the owner's run.

Simulation rules (fixed in the registration): the signal is read on a CLOSED candle and the trade enters at the NEXT candle's open; slippage
is applied against the trade on every entry and exit; fees are charged on both notionals; the stop wins when one candle touches stop and
target; one position per pair at a time; a trade still open at the end of its window is not counted (and the count is reported).
The indicators are computed once over the whole history; they are causal, so each value equals what a prefix would give. The live cycle
sees at most OHLCV_LIMIT candles, so its Wilder smoothing can differ slightly in the third decimal. Funding on Futures is not modelled.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import rsi_mr as R
from .indicators import adx as adx_series
from .indicators import atr as atr_series
from .indicators import rsi as rsi_series
from .strategy import Candle

PROTOCOL = "rsi_mr_v1 backtest, 4 months tuned + 2 months sealed, v1"
PAIRS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT")  # the pairs the bot supports
MARKETS = {"spot": 10.0, "futures": 5.0}  # taker fee per side, basis points
SLIPPAGE_BPS = 5.0  # per side
TOTAL_MONTHS, SEALED_MONTHS, EMBARGO_DAYS = 6.0, 2.0, 3.0
MONTH_MS = int(30.4375 * 86_400_000)
HOUR_MS = 3_600_000
RISK_PCT = 0.5  # the strategy's risk per trade, for the equity curve and drawdown
MIN_TRADES = 30  # fewer trades than this on the tune window: no arm is preferred over the specification
CHALLENGER_MARGIN_R = 0.05  # a variant must beat the specification by this much expectancy (R) ...
CHALLENGER_DD_FACTOR = 1.10  # ... without a drawdown more than 10 percent worse


@dataclass(frozen=True)
class Arm:
    name: str
    params: R.RsiParams
    note: str


ARMS = (
    Arm("SPEC", R.RsiParams(), "the specification: ADX below 25, stop 1.5 ATR, target 1.0 ATR, time stop 24"),
    Arm("ADX20", R.RsiParams(adx_max=20.0), "stricter range filter"),
    Arm("ADX30", R.RsiParams(adx_max=30.0), "looser range filter"),
    Arm("TARGET1.5", R.RsiParams(target_atr=1.5), "larger profit target"),
    Arm("STOP2.0", R.RsiParams(stop_atr=2.0), "wider stop"),
)


# ====================================================================== plan and data identity
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Files whose bytes are part of EVERY backtest registration (audit M3): both runners, the indicator math and the candle type. A change to any of them
# between tuning and the sealed read makes `confirm` refuse. The strategy's own file is added by each runner.
_PINNED = ("rsi_backtest.py", "donchian_backtest.py", "indicators.py", "strategy.py")


def source_pins(strategy_file: str | Path) -> dict:
    here = Path(__file__).parent
    pins = {name: _sha(here / name) for name in _PINNED}
    pins[Path(strategy_file).name] = _sha(Path(strategy_file))
    return pins


def plan_hash() -> str:
    blob = {"protocol": PROTOCOL, "arms": [{"name": a.name, "params": asdict(a.params)} for a in ARMS], "pairs": PAIRS, "markets": MARKETS,
            "slippage_bps": SLIPPAGE_BPS, "months": [TOTAL_MONTHS, SEALED_MONTHS, EMBARGO_DAYS], "risk_pct": RISK_PCT,
            "rule": [MIN_TRADES, CHALLENGER_MARGIN_R, CHALLENGER_DD_FACTOR], "sources_sha256": source_pins(R.__file__), "runner_rules": "next-open entry, stop first"}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]


def kline_path(root: Path, symbol: str) -> Path:
    return Path(root) / "klines" / "1h" / f"{symbol}.csv"


def data_hash(root: Path) -> str:
    h = hashlib.sha256()
    for sym in PAIRS:
        p = kline_path(root, sym)
        if not p.exists():
            raise FileNotFoundError(f"no 1h candles for {sym} under {root}")
        h.update(sym.encode() + _sha(p).encode())
    return h.hexdigest()[:16]


def load_candles(root: Path, symbol: str) -> list[Candle]:
    out: list[Candle] = []
    with kline_path(root, symbol).open("r", newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(Candle(int(row["ts"]), float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"])))
    out.sort(key=lambda c: c.timestamp)
    return out


# ====================================================================== simulation
@dataclass(frozen=True)
class SimTrade:
    pair: str
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    reason: str
    gross_r: float
    net_r: float
    net_pct: float  # return on notional after fees and slippage


def simulate(symbol: str, candles: list[Candle], p: R.RsiParams, market: str, start_ms: int, end_ms: int) -> tuple[list[SimTrade], int]:
    """Trades whose signal candle and exit both lie inside [start_ms, end_ms). Returns (trades, unfinished_count)."""
    closes = [c.close for c in candles]
    r = rsi_series(closes, p.rsi_period)
    a = adx_series([c.high for c in candles], [c.low for c in candles], closes, p.adx_period)
    t = atr_series([c.high for c in candles], [c.low for c in candles], closes, p.atr_period)
    allow_short = market == "futures"
    fee = MARKETS[market] / 1e4
    slip = SLIPPAGE_BPS / 1e4
    trades: list[SimTrade] = []
    unfinished = 0
    i = max(R.min_candles(p), 1)
    n = len(candles)
    while i < n - 1:
        c = candles[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + HOUR_MS > end_ms:
            break
        sig = R.signal_from_values(r[i], r[i - 1], a[i], t[i], c.close, p, allow_short=allow_short)
        if sig.side is None:
            i += 1
            continue
        nxt = candles[i + 1]
        entry = nxt.open * (1 + slip) if sig.side == "long" else nxt.open * (1 - slip)
        stop = R.stop_price(entry, sig.atr, sig.side, p)
        horizon = min(n, i + 2 + p.time_stop_candles + 1)
        ex = R.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=candles[:horizon], tf_ms=HOUR_MS, p=p)
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
        # the next signal can only come from the candle the trade closed on
        j = i + 1
        while j < n and candles[j].timestamp < ex.candle_ts:
            j += 1
        i = max(j, i + 1)
    return trades, unfinished


def metrics(trades: list[SimTrade], start_ms: int, end_ms: int) -> dict:
    weeks = max((end_ms - start_ms) / (7 * 86_400_000), 1e-9)
    n = len(trades)
    out = {"trades": n, "trades_per_week": n / weeks}
    if n == 0:
        return {**out, "win_rate": None, "avg_win_r": None, "avg_loss_r": None, "expectancy_r": None, "expectancy_pct": None, "max_dd_pct": 0.0, "total_net_r": 0.0}
    ordered = sorted(trades, key=lambda x: x.exit_ts)
    wins = [x.net_r for x in ordered if x.net_r > 0]
    losses = [x.net_r for x in ordered if x.net_r <= 0]
    equity, peak, dd = 1.0, 1.0, 0.0
    for x in ordered:
        equity *= 1 + RISK_PCT / 100.0 * x.net_r
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak * 100.0)
    return {**out, "win_rate": len(wins) / n, "avg_win_r": (sum(wins) / len(wins)) if wins else None, "avg_loss_r": (sum(losses) / len(losses)) if losses else None,
            "expectancy_r": sum(x.net_r for x in ordered) / n, "expectancy_pct": sum(x.net_pct for x in ordered) / n * 100.0, "max_dd_pct": dd,
            "total_net_r": sum(x.net_r for x in ordered)}


def run_arm(candles_by_pair: dict[str, list[Candle]], arm: Arm, start_ms: int, end_ms: int) -> dict:
    """{market: {pair|'ALL': metrics, 'unfinished': n, 'by_reason': {...}}}. ALL pools the pairs with no slot limit (an upper bound on frequency)."""
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


# ====================================================================== windows
def windows(candles_by_pair: dict[str, list[Candle]]) -> dict:
    last = min(cs[-1].timestamp for cs in candles_by_pair.values()) + HOUR_MS
    first_needed = last - int(TOTAL_MONTHS * MONTH_MS)
    if min(cs[0].timestamp for cs in candles_by_pair.values()) > first_needed - 14 * 86_400_000:
        raise ValueError(f"the data covers less than {TOTAL_MONTHS:g} months plus warm-up; download more history")
    sealed_start = last - int(SEALED_MONTHS * MONTH_MS)
    tune_end = sealed_start - int(EMBARGO_DAYS * 86_400_000)
    return {"tune": (first_needed, tune_end), "sealed": (sealed_start, last)}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def load_all(root: Path) -> dict[str, list[Candle]]:
    return {sym: load_candles(root, sym) for sym in PAIRS}


# ====================================================================== tune
def select(rows: dict[str, dict]) -> dict:
    """The registered rule. Judged on the STRICTER cost model (Spot), all pairs pooled. The specification stays unless a variant beats it
    on expectancy by CHALLENGER_MARGIN_R without a drawdown more than CHALLENGER_DD_FACTOR worse; with fewer than MIN_TRADES it always stays."""
    spec = rows["SPEC"]["spot"]["ALL"]
    if spec["trades"] < MIN_TRADES:
        return {"arm": "SPEC", "why": f"only {spec['trades']} trades for SPEC on the tuning window (under {MIN_TRADES}): nothing is preferred over the specification"}
    best, best_e = "SPEC", spec["expectancy_r"]
    for name, row in rows.items():
        m = row["spot"]["ALL"]
        if name == "SPEC" or m["trades"] < MIN_TRADES or m["expectancy_r"] is None:
            continue
        if m["expectancy_r"] >= spec["expectancy_r"] + CHALLENGER_MARGIN_R and m["max_dd_pct"] <= spec["max_dd_pct"] * CHALLENGER_DD_FACTOR and m["expectancy_r"] > best_e:
            best, best_e = name, m["expectancy_r"]
    return {"arm": best, "why": "SPEC kept" if best == "SPEC" else f"{best} beat SPEC by at least {CHALLENGER_MARGIN_R} R without a worse drawdown"}


def run_tune(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise ValueError(f"the plan hash {ph} differs from the registered {expect_plan_hash}; nothing was run")
    data = load_all(root)
    w = windows(data)
    t0, t1 = w["tune"]
    rows = {arm.name: run_arm(data, arm, t0, t1) for arm in ARMS}
    chosen = select(rows)
    result = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": data_hash(root), "created": _stamp(), "windows": {k: list(v) for k, v in w.items()}, "rows": rows,
              "selected": chosen, "sealed_months_read": False}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tune_results.json").write_text(json.dumps(result, indent=1))
    (out_dir / "selected.json").write_text(json.dumps({k: result[k] for k in ("protocol", "plan_hash", "data_hash", "windows", "selected")}, indent=1))
    (out_dir / "tune_report.md").write_text(tune_report(result))
    return result


def _fmt(m: dict) -> str:
    if not m["trades"]:
        return "0 trades"
    def f(x, nd=2, pct=False):
        return "n/a" if x is None else (f"{x * 100:.1f}%" if pct else f"{x:+.{nd}f}")
    return (f"{m['trades']} trades ({m['trades_per_week']:.2f}/week), win {f(m['win_rate'], pct=True)}, avg win {f(m['avg_win_r'])} R, avg loss {f(m['avg_loss_r'])} R, "
            f"expectancy {f(m['expectancy_r'], 3)} R ({f(m['expectancy_pct'], 3)}% of notional), max drawdown {m['max_dd_pct']:.1f}%")


def tune_report(res: dict) -> str:
    t0, t1 = res["windows"]["tune"]
    lines = [f"# rsi_mr_v1 backtest: tuning window {_date(t0)} to {_date(t1)} (sealed months NOT read)", "",
             f"Plan hash {res['plan_hash']}, data hash {res['data_hash']}. This is the window parameters were compared on. It is NOT evidence that the strategy works.", "",
             "Expectancy is per trade after fees and slippage. R is the money at risk on the trade (entry to stop).", ""]
    for arm, row in res["rows"].items():
        lines.append(f"## {arm}")
        for market in MARKETS:
            lines.append(f"* {market}, all pairs pooled (no slot limit): {_fmt(row[market]['ALL'])}; unfinished at window end: {row[market]['unfinished']}; exits: {row[market]['by_reason']}")
    lines += ["", "## Selected under the registered rule", f"* {res['selected']['arm']}: {res['selected']['why']}", "",
              "Nothing above says the strategy is profitable. The sealed months are read once, by `confirm`."]
    return "\n".join(lines) + "\n"


# ====================================================================== confirm (one look)
class SealedError(Exception):
    pass


def ledger_path(root: Path, runner: str) -> Path:
    """The one-look ledger sits BESIDE THE DATA (audit M4), one file per runner, keyed on the data hash inside. It is an honesty aid, not a lock: anyone
    who can delete this file can still take a second look; what it prevents is doing so by accident or by choosing a new output folder."""
    return Path(root) / f"sealed_looks.{runner}.json"


def read_looks(root: Path, runner: str, data_hash_: str) -> list:
    p = ledger_path(root, runner)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text()).get(data_hash_, []))
    except (ValueError, AttributeError):
        raise SealedError("the one-look ledger beside the data is unreadable; refusing to open the sealed months")


def record_look(root: Path, runner: str, data_hash_: str, entry: dict) -> int:
    p = ledger_path(root, runner)
    content = json.loads(p.read_text()) if p.exists() else {}
    content.setdefault(data_hash_, []).append(entry)
    p.write_text(json.dumps(content, indent=1))
    return len(content[data_hash_])


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
    looks = read_looks(root, "rsi_mr_v1", dh)
    contaminated = bool(looks)
    if looks and not second_look:
        raise SealedError("the sealed months were already read once; a second look needs --second-look and is labelled contaminated")
    n_looks = record_look(root, "rsi_mr_v1", dh, {"ts": _stamp(), "reason": reason.strip(), "plan_hash": ph, "second_look": contaminated})  # BEFORE anything is read
    s0, s1 = w["sealed"]
    arms = {a.name: a for a in ARMS}
    names = ["SPEC"] if chosen["selected"]["arm"] == "SPEC" else ["SPEC", chosen["selected"]["arm"]]
    rows = {n: run_arm(data, arms[n], s0, s1) for n in names}
    result = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": dh, "created": _stamp(), "sealed_window": [s0, s1], "selected": chosen["selected"], "rows": rows,
              "contaminated": contaminated, "looks": n_looks}
    (out_dir / "sealed_results.json").write_text(json.dumps(result, indent=1))
    (out_dir / "final_report.md").write_text(final_report(result))
    return result


def final_report(res: dict) -> str:
    s0, s1 = res["sealed_window"]
    lines = [f"# rsi_mr_v1 backtest: final report, sealed window {_date(s0)} to {_date(s1)}", "",
             f"Plan hash {res['plan_hash']}, data hash {res['data_hash']}. These two months were read ONCE after the parameters were fixed on the earlier four.", ""]
    if res["contaminated"]:
        lines += ["**WARNING: this was NOT the first look at the sealed months. The result is no longer unseen evidence.**", ""]
    negative = []
    for arm, row in res["rows"].items():
        lines.append(f"## {arm}" + (" (selected on the tuning window)" if arm == res["selected"]["arm"] else " (the specification)"))
        for market in MARKETS:
            lines.append(f"### {market}")
            lines.append(f"* All pairs pooled (no slot limit, an upper bound on frequency): {_fmt(row[market]['ALL'])}")
            for sym in PAIRS:
                lines.append(f"* {sym}: {_fmt(row[market][sym])}")
            lines.append(f"* Exits: {row[market]['by_reason']}; unfinished at window end (not counted): {row[market]['unfinished']}")
            e = row[market]["ALL"]["expectancy_r"]
            if e is not None and e < 0:
                negative.append(f"{arm} on {market}")
        lines.append("")
    if negative:
        lines += ["## Result", "**The untouched two months are NEGATIVE after fees and slippage for: " + "; ".join(negative) + ". The strategy lost money there.**", ""]
    else:
        lines += ["## Result", "Expectancy after fees and slippage was not negative on the untouched months for the arms shown. Two months is short: this can be luck, "
                  "and it says nothing about other regimes.", ""]
    lines += ["## Combined trades per week for all three strategies", "NOT MEASURED. This runner measures rsi_mr_v1 only. ema_cross_atr_v1 is measured by the research branch engine, "
              "and donchian_breakout_v1 does not exist yet. The pooled rsi_mr_v1 figure above is the only frequency measured here.", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.rsi_backtest", description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["plan", "tune", "confirm"])
    ap.add_argument("--root", default="research_data")
    ap.add_argument("--out", default="research_out/rsi_mr")
    ap.add_argument("--expect-plan-hash")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--second-look", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.action == "plan":
            print(json.dumps({"plan_hash": plan_hash(), "arms": [x.name for x in ARMS], "pairs": PAIRS, "markets": MARKETS, "slippage_bps": SLIPPAGE_BPS,
                              "months": [TOTAL_MONTHS, SEALED_MONTHS, EMBARGO_DAYS]}, indent=1))
            return 0
        if a.action == "tune":
            run_tune(Path(a.root), Path(a.out), expect_plan_hash=a.expect_plan_hash)
            print((Path(a.out) / "tune_report.md").read_text())
            return 0
        run_confirm(Path(a.root), Path(a.out), reason=a.reason, confirm=a.confirm, second_look=a.second_look)
        print((Path(a.out) / "final_report.md").read_text())
        return 0
    except (SealedError, ValueError, FileNotFoundError) as exc:
        print(f"rsi_backtest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
