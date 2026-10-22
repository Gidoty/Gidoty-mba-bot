"""Data-quality checks for the local market-data store.

Research conclusions are only as good as the candles under them, so every file
gets an explicit verdict before any backtest uses it:

  fail  duplicate or out-of-order timestamps, impossible OHLC, non-positive prices
  warn  missing bars (gaps), zero-volume bars, one-bar moves over 25%, less history
        than the target (e.g. a pair listed less than three years ago)
  ok    none of the above

A `warn` is not hidden: it is reported with counts and the first gaps, and the
backtester can be told to refuse files that are not `ok` (`strict`).
"""
from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .data import INTERVAL_MS, funding_path, kline_path, premium_path, symbol_for

YEAR_MS = 365.25 * 86_400_000
EXTREME_BAR_MOVE = 0.25


@dataclass
class FileQuality:
    path: str
    kind: str
    rows: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    years: float = 0.0
    duplicates: int = 0
    out_of_order: int = 0
    missing_bars: int = 0
    gaps: list[list[int]] = field(default_factory=list)  # [start_ts, missing_bar_count], first 10
    bad_ohlc: int = 0
    non_positive: int = 0
    zero_volume: int = 0
    extreme_moves: int = 0
    coverage_pct: float | None = None
    median_step_ms: int | None = None
    irregular_steps: int = 0
    status: str = "ok"
    notes: list[str] = field(default_factory=list)


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else None


def _read(path: Path) -> list[dict]:
    with path.open("r", newline="") as fh:
        return list(csv.DictReader(fh))


def _finish(q: FileQuality, *, min_years: float) -> FileQuality:
    if q.first_ts is not None and q.last_ts is not None:
        q.years = round((q.last_ts - q.first_ts) / YEAR_MS, 3)
    if q.duplicates or q.out_of_order or q.bad_ohlc or q.non_positive:
        q.status = "fail"
    elif q.missing_bars or q.zero_volume or q.extreme_moves or q.irregular_steps or q.years < min_years:
        q.status = "warn"
    if q.years < min_years:
        q.notes.append(f"history is {q.years} years, target {min_years}")
    return q


def check_klines(path: Path, interval: str, *, kind: str = "klines", min_years: float = 3.0) -> FileQuality:
    q = FileQuality(path=str(path), kind=kind)
    if not path.exists():
        q.status = "fail"
        q.notes.append("file is missing")
        return q
    step = INTERVAL_MS[interval]
    rows = _read(path)
    q.rows = len(rows)
    if not rows:
        q.status = "fail"
        q.notes.append("file is empty")
        return q
    prev_ts = None
    prev_close = None
    for r in rows:
        ts = int(r["ts"])
        if prev_ts is not None:
            if ts == prev_ts:
                q.duplicates += 1
            elif ts < prev_ts:
                q.out_of_order += 1
            elif ts - prev_ts > step:
                missing = (ts - prev_ts) // step - 1
                q.missing_bars += missing
                if len(q.gaps) < 10:
                    q.gaps.append([prev_ts + step, missing])
        o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        if kind == "premium":
            # A premium index is a small signed fraction, not a price: only the
            # ordering of its own high and low can be checked.
            if h < lo - 1e-12:
                q.bad_ohlc += 1
            prev_ts = ts
            continue
        if min(o, h, lo, c) <= 0:
            q.non_positive += 1
        elif h < max(o, c, lo) - 1e-12 or lo > min(o, c, h) + 1e-12:
            q.bad_ohlc += 1
        if kind == "klines" and float(r.get("volume") or 0) == 0:
            q.zero_volume += 1
        if prev_close and prev_close > 0 and abs(c / prev_close - 1.0) > EXTREME_BAR_MOVE:
            q.extreme_moves += 1
        prev_ts, prev_close = ts, c
    q.first_ts, q.last_ts = int(rows[0]["ts"]), int(rows[-1]["ts"])
    expected = (q.last_ts - q.first_ts) // step + 1
    q.coverage_pct = round(100.0 * (q.rows - q.duplicates) / expected, 4) if expected else None
    return _finish(q, min_years=min_years)


def check_funding(path: Path, *, min_years: float = 3.0) -> FileQuality:
    q = FileQuality(path=str(path), kind="funding")
    if not path.exists():
        q.status = "fail"
        q.notes.append("file is missing")
        return q
    rows = _read(path)
    q.rows = len(rows)
    if not rows:
        q.status = "fail"
        q.notes.append("file is empty")
        return q
    ts = [int(r["ts"]) for r in rows]
    q.first_ts, q.last_ts = ts[0], ts[-1]
    diffs = [b - a for a, b in zip(ts, ts[1:])]
    q.duplicates = sum(1 for d in diffs if d == 0)
    q.out_of_order = sum(1 for d in diffs if d < 0)
    positive = [d for d in diffs if d > 0]
    if positive:
        q.median_step_ms = int(statistics.median(positive))
        # Funding times are not exact to the millisecond; allow a minute of jitter
        # and treat a change of schedule (8h -> 4h) as irregular, not as an error.
        q.irregular_steps = sum(1 for d in positive if abs(d - q.median_step_ms) > 60_000)
        q.missing_bars = sum(max(round(d / q.median_step_ms) - 1, 0) for d in positive if d > q.median_step_ms * 1.5)
    rates = [abs(float(r["rate"])) for r in rows]
    if rates and max(rates) > 0.02:
        q.notes.append(f"largest single funding payment {max(rates):.4%} of notional")
    return _finish(q, min_years=min_years)


def check_store(root: Path, *, pairs: list[str], intervals: tuple[str, ...] = ("1h",), min_years: float = 3.0) -> dict:
    root = Path(root)
    files: list[FileQuality] = []
    for pair in pairs:
        sym = symbol_for(pair)
        for interval in intervals:
            files.append(check_klines(kline_path(root, interval, sym), interval, min_years=min_years))
        premium = premium_path(root, "1h", sym)
        if premium.exists():
            files.append(check_klines(premium, "1h", kind="premium", min_years=min_years))
        files.append(check_funding(funding_path(root, sym), min_years=min_years))
    summary = {s: sum(1 for f in files if f.status == s) for s in ("ok", "warn", "fail")}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_years_target": min_years,
        "summary": summary,
        "files": [
            {**asdict(f), "first": _iso(f.first_ts), "last": _iso(f.last_ts), "path": str(Path(f.path).relative_to(root)) if Path(f.path).is_relative_to(root) else f.path}
            for f in files
        ],
    }
    (root / "quality.json").write_text(json.dumps(report, indent=1))
    return report


def render(report: dict) -> str:
    lines = [f"Data quality: {report['summary']['ok']} ok, {report['summary']['warn']} warn, {report['summary']['fail']} fail"]
    for f in report["files"]:
        bits = [f"{f['rows']:>7} rows", f"{f['years']:.2f}y", f"{f['first']} -> {f['last']}"]
        if f["missing_bars"]:
            bits.append(f"{f['missing_bars']} missing")
        if f["zero_volume"]:
            bits.append(f"{f['zero_volume']} zero-vol")
        if f["extreme_moves"]:
            bits.append(f"{f['extreme_moves']} >25% bars")
        if f["duplicates"] or f["out_of_order"] or f["bad_ohlc"] or f["non_positive"]:
            bits.append("INTEGRITY ERRORS")
        lines.append(f"  [{f['status']:>4}] {f['path']:<34} " + ", ".join(bits) + ("  | " + "; ".join(f["notes"]) if f["notes"] else ""))
    return "\n".join(lines)
