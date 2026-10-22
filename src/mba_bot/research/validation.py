"""Guards against fooling ourselves: walk-forward folds, a lockbox, a ledger of every
experiment tried, a deflated Sharpe ratio, and a pass/fail gate fixed BEFORE the results are seen.

The danger is not a bug in the backtester, it is trying many variants and keeping the one that
happened to look best. The pieces here make that visible and expensive:

* TrialRegistry   every configuration evaluated is appended to a ledger. The number of distinct
                  configurations tried in a family is the "number of trials" the deflated Sharpe
                  ratio needs, so it cannot be quietly understated.
* Lockbox         the most recent months and a set of holdout pairs are reserved. Opening the
                  lockbox is explicit and logged; opening it twice marks it contaminated.
* walk_forward    parameters are chosen on a training window only, then scored on the following
                  unseen window, rolling forward; with an embargo gap between them.
* deflated_sharpe PSR against the Sharpe the best of N random strategies would show by luck
                  (Bailey and Lopez de Prado, 2014).
* PassCriteria    thresholds written down in advance. A variant either meets them or does not.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Iterable

DAY_MS = 86_400_000
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


# ------------------------------------------------------------------ deflated Sharpe


def sample_skew_kurtosis(values: list[float]) -> tuple[float, float]:
    """Skewness and (raw) kurtosis; a normal distribution has kurtosis 3."""
    n = len(values)
    if n < 4:
        return 0.0, 3.0
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    if sd == 0:
        return 0.0, 3.0
    skew = sum(((v - mean) / sd) ** 3 for v in values) / n
    kurt = sum(((v - mean) / sd) ** 4 for v in values) / n
    return skew, kurt


def probabilistic_sharpe_ratio(sr: float, benchmark: float, n_obs: int, skew: float, kurt: float) -> float | None:
    """P(true Sharpe > benchmark). `sr` and `benchmark` are per-observation (NOT annualised)."""
    if n_obs < 3:
        return None
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return None
    return _N.cdf((sr - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, trial_sharpe_variance: float) -> float:
    """The Sharpe the best of `n_trials` strategies with no real skill is expected to show."""
    if n_trials <= 1 or trial_sharpe_variance <= 0:
        return 0.0
    return math.sqrt(trial_sharpe_variance) * (
        (1.0 - EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / n_trials) + EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe_ratio(daily_returns: list[float], n_trials: int, trial_sharpes: list[float] | None = None) -> dict:
    """DSR on DAILY returns. `trial_sharpes` are the per-day Sharpe ratios of the other configurations
    tried (their variance sets how lucky a best-of-N can look). Without them the estimate uses the
    candidate's own sampling variance, which is less conservative, and says so."""
    n = len(daily_returns)
    if n < 30:
        return {"dsr": None, "note": f"needs >= 30 daily observations, have {n}"}
    sd = statistics.pstdev(daily_returns)
    if sd == 0:
        return {"dsr": None, "note": "zero-variance returns"}
    sr = statistics.fmean(daily_returns) / sd
    skew, kurt = sample_skew_kurtosis(daily_returns)
    if trial_sharpes and len(trial_sharpes) >= 2:
        var = statistics.pvariance(trial_sharpes)
        basis = "variance of the Sharpe ratios of the configurations tried"
    else:
        var = (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / (n - 1)
        basis = "the candidate's own sampling variance (no other trials recorded: less conservative)"
    bench = expected_max_sharpe(max(n_trials, 1), var)
    dsr = probabilistic_sharpe_ratio(sr, bench, n, skew, kurt)
    return {
        "dsr": dsr, "sharpe_daily": sr, "sharpe_annualised": sr * math.sqrt(365.0), "benchmark_daily": bench,
        "benchmark_annualised": bench * math.sqrt(365.0), "n_trials": n_trials, "n_days": n, "skew": skew, "kurtosis": kurt,
        "variance_basis": basis,
        "reading": "probability the true Sharpe beats what the best of the trials would show by luck; >= 0.95 is the usual bar",
    }


# ------------------------------------------------------------------ ledger


def config_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def code_version(repo: Path | None = None) -> str:
    try:
        cwd = str(repo) if repo else None
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=5, cwd=cwd).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=5, cwd=cwd).stdout.strip())
        return (sha or "unknown") + ("+dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


class TrialRegistry:
    """Append-only JSONL ledger of experiments."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, record: dict) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def record_trial(self, *, family: str, config: Any, data_hash: str, summary: dict, window: str = "full", note: str = "") -> str:
        h = config_hash(config)
        self._append({"kind": "trial", "family": family, "config_hash": h, "config": config, "data_hash": data_hash,
                      "window": window, "code_version": code_version(), "summary": summary, "note": note})
        return h

    def n_trials(self, family: str) -> int:
        return len({r["config_hash"] for r in self.records() if r["kind"] == "trial" and r["family"] == family})

    def trial_sharpes(self, family: str) -> list[float]:
        """Per-day Sharpe of each distinct configuration (its latest full-window record)."""
        latest: dict[str, float] = {}
        for r in self.records():
            if r["kind"] == "trial" and r["family"] == family and r.get("window") == "full":
                s = (r.get("summary") or {}).get("sharpe_daily")
                if s is not None:
                    latest[r["config_hash"]] = s
        return list(latest.values())

    def lockbox_openings(self, family: str) -> int:
        return sum(1 for r in self.records() if r["kind"] == "lockbox_open" and r["family"] == family)

    def record_lockbox_open(self, *, family: str, reason: str, window: tuple[int, int]) -> int:
        self._append({"kind": "lockbox_open", "family": family, "reason": reason, "window": list(window)})
        return self.lockbox_openings(family)


class LookLedger:
    """Append-only record of every time a dataset (or a slice of one) was examined, and what for.

    The trial registry counts configurations. This counts LOOKS: a result that was seen cannot be treated as unseen
    evidence later. Each entry says which pairs and period were looked at, the purpose, and whether the look could
    influence design (`kind`: "design" informed choices, "descriptive" was only inspected, "confirmation" was a
    pre-registered one-shot test). Nothing is ever edited or removed."""

    KINDS = ("design", "descriptive", "confirmation")

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *, dataset: str, pairs: list[str], period: str, kind: str, purpose: str, plan_hash: str = "", note: str = "") -> dict:
        if kind not in self.KINDS:
            raise ValueError(f"look kind must be one of {self.KINDS}")
        row = {"ts": datetime.now(timezone.utc).isoformat(), "dataset": dataset, "pairs": sorted(pairs), "period": period, "kind": kind,
               "purpose": purpose, "plan_hash": plan_hash, "note": note}
        with self.path.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def confirmations(self, dataset: str) -> list[dict]:
        return [e for e in self.entries() if e["dataset"] == dataset and e["kind"] == "confirmation"]

    def assert_unconsumed(self, dataset: str) -> None:
        """One-shot evidence: refuse a second confirmation look at the same dataset."""
        if self.confirmations(dataset):
            raise LockboxError(f"dataset {dataset!r} was already consumed as confirmation evidence; it is no longer unseen")


#: What was examined before Phase 2, reconstructed from the committed history. Written into every Phase 2 ledger.
RETROSPECTIVE_LOOKS = (
    dict(dataset="phase1_store", pairs=["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT"], period="development range (all but the sealed lockbox)",
         kind="design", purpose="Phase 0 baseline and Phase 1 candidate search: 29 candidates, 33 trials, none passed C1-C9"),
    dict(dataset="phase1_store", pairs=["SOL/USDT", "DOGE/USDT", "SUI/USDT", "LINK/USDT", "ADA/USDT", "LTC/USDT"], period="development range (all but the sealed lockbox)",
         kind="descriptive", purpose="Phase 0/1 baseline and descriptive studies only. No Phase 1 candidate was run on these pairs"),
    dict(dataset="phase1_lockbox", pairs=["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT", "SUI/USDT", "LINK/USDT", "ADA/USDT", "LTC/USDT"],
         period="final months of the Phase 1 store", kind="design", purpose="NEVER OPENED. Listed so its sealed state is on the record", note="sealed"),
)


# ------------------------------------------------------------------ lockbox


class LockboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lockbox:
    """The final `months` of data and a set of holdout pairs, kept out of development."""

    first_ts: int
    last_ts: int
    months: float = 6.0
    holdout_symbols: frozenset[str] = frozenset()
    embargo_days: float = 3.0

    @property
    def start_ts(self) -> int:
        return int(self.last_ts - self.months * 30.4375 * DAY_MS)

    def develop_range(self) -> tuple[int, int]:
        """Everything before the lockbox, with an embargo so no trade can straddle the boundary."""
        return self.first_ts, int(self.start_ts - self.embargo_days * DAY_MS)

    def lockbox_range(self) -> tuple[int, int]:
        return self.start_ts, self.last_ts

    def assert_tunable(self, symbols: Iterable[str]) -> None:
        clash = sorted(set(symbols) & set(self.holdout_symbols))
        if clash:
            raise LockboxError(f"{clash} are holdout pairs and must not be used for tuning or selection")

    def open(self, registry: TrialRegistry, *, family: str, reason: str, confirm: bool) -> dict:
        if not confirm:
            raise LockboxError("opening the lockbox must be explicit: pass confirm=True and a reason")
        if not reason.strip():
            raise LockboxError("a reason is required")
        count = registry.record_lockbox_open(family=family, reason=reason, window=self.lockbox_range())
        return {"range": self.lockbox_range(), "opening_number": count, "contaminated": count > 1,
                "warning": None if count == 1 else "the lockbox has been opened before: this is no longer a clean out-of-sample test"}


# ------------------------------------------------------------------ walk-forward


@dataclass(frozen=True)
class Fold:
    index: int
    train: tuple[int, int]
    test: tuple[int, int]


def make_folds(first_ts: int, last_ts: int, *, train_days: float, test_days: float, embargo_days: float = 1.0, anchored: bool = False) -> list[Fold]:
    """Rolling (or anchored) train/test windows. Test windows tile the timeline without overlapping;
    `embargo_days` separate the end of training from the start of testing."""
    train_ms, test_ms, emb_ms = int(train_days * DAY_MS), int(test_days * DAY_MS), int(embargo_days * DAY_MS)
    folds = []
    test_start = first_ts + train_ms + emb_ms
    i = 0
    while test_start + test_ms <= last_ts:
        train_end = test_start - emb_ms
        train_start = first_ts if anchored else train_end - train_ms
        folds.append(Fold(i, (train_start, train_end), (test_start, test_start + test_ms)))
        test_start += test_ms
        i += 1
    return folds


Evaluator = Callable[[dict, int, int], dict]


def walk_forward(
    evaluate: Evaluator,
    grid: list[dict],
    folds: list[Fold],
    *,
    registry: TrialRegistry | None = None,
    family: str = "walk_forward",
    data_hash: str = "",
    objective: str = "expectancy_r",
    min_trades: int = 20,
) -> dict:
    """`evaluate(params, start_ts, end_ts)` returns a summary dict containing at least
    `n_trades` and the objective. Parameters are chosen on the training window only."""
    if not folds:
        raise ValueError("no folds: the data span is shorter than one train + test window")
    rows = []
    for fold in folds:
        scored = []
        for params in grid:
            train = evaluate(params, *fold.train)
            value = train.get(objective)
            if train.get("n_trades", 0) >= min_trades and value is not None:
                scored.append((value, params, train))
        if not scored:
            rows.append({"fold": fold.index, "train": fold.train, "test": fold.test, "chosen": None, "reason": f"no configuration reached {min_trades} training trades"})
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        value, params, train = scored[0]
        test = evaluate(params, *fold.test)
        rows.append({
            "fold": fold.index, "train": fold.train, "test": fold.test, "chosen": params,
            "train_value": value, "train_trades": train.get("n_trades"),
            "test_value": test.get(objective), "test_trades": test.get("n_trades"),
            "test_net_pnl": test.get("net_pnl"),
        })
    if registry is not None:
        for params in grid:
            registry.record_trial(family=family, config=params, data_hash=data_hash, summary={"in_walk_forward": True}, window="walk_forward")
    used = [r for r in rows if r.get("chosen")]
    test_vals = [r["test_value"] for r in used if r.get("test_value") is not None]
    train_vals = [r["train_value"] for r in used]
    chosen_counts: dict[str, int] = {}
    for r in used:
        key = json.dumps(r["chosen"], sort_keys=True)
        chosen_counts[key] = chosen_counts.get(key, 0) + 1
    return {
        "objective": objective, "n_folds": len(folds), "n_folds_used": len(used), "n_configurations": len(grid), "rows": rows,
        "positive_test_fraction": (sum(1 for v in test_vals if v > 0) / len(test_vals)) if test_vals else None,
        "mean_train_value": statistics.fmean(train_vals) if train_vals else None,
        "mean_test_value": statistics.fmean(test_vals) if test_vals else None,
        "walk_forward_efficiency": (statistics.fmean(test_vals) / statistics.fmean(train_vals)) if test_vals and train_vals and statistics.fmean(train_vals) not in (0, None) else None,
        "parameter_stability": {"distinct_choices": len(chosen_counts), "most_common_share": (max(chosen_counts.values()) / len(used)) if used else None},
        "note": "a walk-forward efficiency well below 1 means the in-sample edge did not carry over; with many configurations the winner is selected on noise",
    }


# ------------------------------------------------------------------ pass/fail gate


@dataclass(frozen=True)
class PassCriteria:
    """Written down BEFORE a candidate is run. A change must meet every line."""

    min_trades: int = 100
    min_expectancy_r_net: float = 0.0
    min_positive_fold_fraction: float = 0.70
    cost_stress_multiplier: float = 1.5
    min_expectancy_r_under_stress: float = 0.0
    max_drawdown_worsening_pct_points: float = 0.0
    min_deflated_sharpe: float = 0.95
    must_not_be_worse_than_baseline_on_holdout_pairs: bool = True


def evaluate_gate(
    criteria: PassCriteria, *, candidate: dict, baseline: dict, stressed: dict | None, walk_forward_report: dict | None,
    dsr: dict | None, holdout_candidate: dict | None = None, holdout_baseline: dict | None = None,
) -> dict:
    checks = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"check": name, "pass": ok, "detail": detail})

    n = candidate.get("n_trades", 0)
    add("enough trades", n >= criteria.min_trades, f"{n} trades (need {criteria.min_trades})")
    e = candidate.get("expectancy_r")
    add("positive net expectancy", e is not None and e > criteria.min_expectancy_r_net, f"{e}")
    if stressed is not None:
        es = stressed.get("expectancy_r")
        add(f"survives {criteria.cost_stress_multiplier}x costs", es is not None and es > criteria.min_expectancy_r_under_stress, f"{es}")
    else:
        add(f"survives {criteria.cost_stress_multiplier}x costs", None, "cost-stress run not supplied")
    if walk_forward_report is not None:
        frac = walk_forward_report.get("positive_test_fraction")
        add("positive in enough out-of-sample folds", frac is not None and frac >= criteria.min_positive_fold_fraction, f"{frac}")
    else:
        add("positive in enough out-of-sample folds", None, "walk-forward not supplied")
    cd, bd = candidate.get("max_drawdown_pct"), baseline.get("max_drawdown_pct")
    if cd is not None and bd is not None:
        add("drawdown not worse than baseline", cd <= bd + criteria.max_drawdown_worsening_pct_points, f"{cd:.2f}% vs {bd:.2f}%")
    d = (dsr or {}).get("dsr")
    add("deflated Sharpe", None if d is None else d >= criteria.min_deflated_sharpe, f"{d}")
    if criteria.must_not_be_worse_than_baseline_on_holdout_pairs and holdout_candidate and holdout_baseline:
        hc, hb = holdout_candidate.get("expectancy_r"), holdout_baseline.get("expectancy_r")
        add("not worse than baseline on holdout pairs", hc is not None and hb is not None and hc >= hb, f"{hc} vs {hb}")
    failed = [c["check"] for c in checks if c["pass"] is False]
    undetermined = [c["check"] for c in checks if c["pass"] is None]
    verdict = "fail" if failed else ("incomplete" if undetermined else "pass")
    return {"verdict": verdict, "failed": failed, "undetermined": undetermined, "checks": checks, "criteria": asdict(criteria)}
