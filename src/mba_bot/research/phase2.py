"""Phase 2: one-shot confirmation of the frozen Phase 1 near-misses on data nobody has examined.

Nothing here changes the service. The arms, segments, statistics and pass/fail rules below are fixed BEFORE the older
history is downloaded or looked at. `plan_hash()` fingerprints all of them, `code_fingerprint()` fingerprints the source
files that decide the outcome; both are printed with every run and committed in docs/research/phase2_preregistration.md.
`run` refuses to start if either differs from what the operator expects, or if the older-history store was already
consumed under a different plan.

What the three segments are
  A  the four development pairs on the OLDER history (before the Phase 1 store begins). Time out-of-sample. PRIMARY.
  B  the six added pairs on the Phase 1 store, development period only. Asset out-of-sample, but their baseline and
     descriptive results were already seen in Phase 0/1, so after this test they are no longer clean holdouts.
  C  the six added pairs on the OLDER history. Time and asset out-of-sample (pairs listed later have no older history).

The Phase 1 lockbox is never opened here. No candidate reaches production from this test alone: a forward shadow period
is still required.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import confirm_stats as cs, data as datalib, quality, synthetic
from .engine import MODE_REPLAY, AccountParams, CostModel, Policy, StrategyParams, TradeRecord
from .metrics import trade_stats
from .phase1 import EXTRA_SIX, ORIGINAL_FOUR, Lab, Phase1Options, RunDigest
from .reporting import data_hash
from .validation import RETROSPECTIVE_LOOKS, LockboxError, LookLedger, code_version

DAY_MS = 86_400_000
FAMILY = "phase2"

# ------------------------------------------------------------------ the frozen plan


@dataclass(frozen=True)
class Arm:
    id: str
    label: str
    policy: Policy
    role: str  # baseline | primary | sizing | sensitivity | diagnostic
    hypothesis: str = ""
    phase1_id: str | None = None  # the Phase 1 candidate this arm re-tests (for the shrinkage check)


_V67 = Policy(vol_skip_above=0.67)
_SM10 = Policy(confirm_margin_atr=0.10, confirm_sides=("short",))
_XC = Policy(exit_on_close_only=True)

ARMS: tuple[Arm, ...] = (
    Arm("BASE", "production behaviour", Policy(), "baseline"),
    Arm("V67", "skip entries above the 67th volatility percentile", _V67, "primary", "P2-H1", "H2-V67"),
    Arm("SM10", "shorts need an EMA gap of 0.10 ATR", _SM10, "primary", "P2-H2", "H3-SM10"),
    Arm("XC", "act on the opposite crossover only when the hourly candle closes", _XC, "primary", "P2-H3", "H4-XC"),
    Arm("COMBO", "V67 + SM10 + XC together", Policy(vol_skip_above=0.67, confirm_margin_atr=0.10, confirm_sides=("short",), exit_on_close_only=True), "primary", "P2-H4"),
    Arm("SHORT_HALF", "shorts are sized at half the normal risk", Policy(short_risk_scale=0.5), "sizing", "P2-H5"),
    Arm("V80", "SENSITIVITY: skip entries above the 80th volatility percentile", Policy(vol_skip_above=0.80), "sensitivity", "", "H2-V80"),
    Arm("NOSHORT", "DIAGNOSTIC: no shorts (measures what shorts contribute)", Policy(short_allowed=False), "diagnostic", "", "D-NOSHORT"),
)
ARM_BY_ID = {a.id: a for a in ARMS}
PRIMARY = tuple(a.id for a in ARMS if a.role == "primary")
AUTO_SEEDS = tuple(range(1, 21))  # random-ranking benchmark: 20 seeds
COST_POINTS = (0.5, 1.0, 2.0, 3.0)  # cost multipliers; 3x is the pre-registered stress, 2x is criterion K9
COST_ARMS = ("BASE",) + PRIMARY
A_PAIRS, B_PAIRS, C_PAIRS = tuple(ORIGINAL_FOUR), tuple(EXTRA_SIX), tuple(EXTRA_SIX)

SEGMENT_DOC = {
    "A": "four development pairs, older history (before the Phase 1 store begins). Time out-of-sample. PRIMARY evidence.",
    "B": "six added pairs, Phase 1 store, development period only. Asset out-of-sample; baseline/descriptive results were already seen in Phase 0/1.",
    "C": "six added pairs, older history (pairs with no older history are skipped). Time and asset out-of-sample.",
}


@dataclass(frozen=True)
class Criteria:
    holm_alpha: float = 0.10  # K2: Holm-adjusted one-sided paired block-bootstrap p-value, family of the 4 primary arms
    block_days: int = 14
    bootstrap_iterations: int = 5000
    bootstrap_seed: int = 7
    min_shrinkage: float = 0.50  # K3: segment A improvement is at least this share of the Phase 1 improvement
    min_big_winner_ratio: float = 0.85  # K4: share of >= 3R trades kept
    min_top_decile_ratio: float = 0.85  # K4: top-decile mean R kept
    max_drawdown_ratio: float = 1.10  # K5: worst-pair max drawdown at most 110% of the baseline's
    min_pairs_positive: int = 3  # K6: pairs (of 4) where the arm beats the baseline
    min_c_baseline_trades: int = 100  # K7: segment C is judged only if it has this many baseline trades
    regime_min_trades: int = 30  # K8: a regime counts only with this many baseline trades
    regime_floor_r: float = -0.10  # K8: no counted regime may be worse than this
    regime_min_positive: int = 2  # K8: counted regimes with a positive delta
    cost_check_multiplier: float = 2.0  # K9
    event_quantile: float = 0.99  # K10: top 1% BTC absolute daily moves are event days
    sizing_max_return_loss: float = 0.25  # S3: shorts-at-half-risk may give up at most this share of the baseline return
    auto_percentile: float = 0.90  # AU: production ranking must beat this percentile of the random seeds


CRITERIA = Criteria()


@dataclass(frozen=True)
class Phase2Options:
    a_pairs: tuple[str, ...] = A_PAIRS
    b_pairs: tuple[str, ...] = B_PAIRS
    c_pairs: tuple[str, ...] = C_PAIRS
    arms: tuple[Arm, ...] = ARMS
    seeds: tuple[int, ...] = AUTO_SEEDS
    cost_points: tuple[float, ...] = COST_POINTS
    criteria: Criteria = CRITERIA
    account: AccountParams = field(default_factory=lambda: AccountParams(allocated_capital=1000.0))
    costs: CostModel = field(default_factory=CostModel)
    strategy: StrategyParams = field(default_factory=StrategyParams)


def plan_hash(options: Phase2Options | None = None) -> str:
    o = options or Phase2Options()
    blob = {"arms": [asdict(a) for a in o.arms], "segments": {"A": o.a_pairs, "B": o.b_pairs, "C": o.c_pairs}, "seeds": o.seeds,
            "cost_points": o.cost_points, "cost_arms": COST_ARMS, "criteria": asdict(o.criteria), "account": asdict(o.account),
            "costs": asdict(o.costs), "strategy": asdict(o.strategy), "protocol": PROTOCOL_VERSION}
    return hashlib.sha1(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


PROTOCOL_VERSION = "phase2-v1"

#: The source files whose behaviour decides the outcome. Their bytes are hashed; the hash is committed in the registration.
FINGERPRINTED_FILES = ("engine.py", "tape.py", "data.py", "metrics.py", "analysis.py", "confirm_stats.py", "phase1.py", "phase2.py", "validation.py", "reporting.py")


def code_fingerprint() -> str:
    h = hashlib.sha256()
    here = Path(__file__).parent
    for name in FINGERPRINTED_FILES:
        h.update(name.encode() + b"\0" + (here / name).read_bytes() + b"\0")
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ the Phase 1 reference (read from the artifact, never typed)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_phase1_reference(path: Path, *, expect_sha256: str | None) -> dict:
    """Read the exact Phase 1 outputs that Phase 2 depends on. The file is pinned by SHA-256, so the numbers the runner
    uses are the ones that were reviewed, not retyped ones."""
    path = Path(path)
    digest = file_sha256(path)
    if expect_sha256 and digest != expect_sha256:
        raise RuntimeError(f"phase1_results.json hashes to {digest}, not the pinned {expect_sha256}: refusing to use it")
    res = json.loads(path.read_text())
    meta = res["meta"]
    deltas, per_pair = {}, {}
    for cid, entry in res.get("candidates", {}).items():
        cmp_ = entry.get("compare") or {}
        deltas[cid] = {"delta_r": cmp_.get("delta_r"), "candidate_r": (cmp_.get("candidate") or {}).get("expectancy_r"),
                       "baseline_r": (cmp_.get("baseline") or {}).get("expectancy_r"), "n_candidate": (cmp_.get("candidate") or {}).get("n_trades"),
                       "n_baseline": (cmp_.get("baseline") or {}).get("n_trades"), "failed_checks": entry.get("failed_checks")}
        per_pair[cid] = {s: v.get("delta") for s, v in (cmp_.get("per_pair") or {}).items()}
    return {"sha256": digest, "plan_hash": meta.get("plan_hash"), "data_fingerprint": meta.get("data_fingerprint"), "span": meta["span"],
            "development_range": meta["development_range"], "lockbox_range": meta["lockbox_range"], "dev_pairs": meta.get("dev_pairs"),
            "holdout_pairs": meta.get("holdout_pairs"), "n_trials": meta.get("n_trials"), "deltas": deltas, "per_pair_deltas": per_pair}


# ------------------------------------------------------------------ guards for the one-shot data


def guard_download(*, expect_plan_hash: str | None, options: Phase2Options | None = None) -> None:
    """The older history may be fetched only after this plan has been registered. The operator passes the hash from the
    committed registration; any change to the plan changes the hash and the download refuses."""
    actual = plan_hash(options)
    if not expect_plan_hash:
        raise RuntimeError("downloading the older history requires --expect-plan-hash (the hash in docs/research/phase2_preregistration.md)")
    if actual != expect_plan_hash:
        raise RuntimeError(f"the plan hashes to {actual}, not the registered {expect_plan_hash}: the registration and the code disagree")


MARKER = "PHASE2_CONSUMED.json"


def claim_older_store(root: Path, *, plan: str, fingerprint: str) -> dict:
    """Mark the older store as examined by this plan. Re-running the SAME frozen plan and code is the same test (a crashed
    run may be repeated); a different plan or code would be a second look and is refused."""
    root = Path(root)
    marker = root / MARKER
    if marker.exists():
        prev = json.loads(marker.read_text())
        if prev["plan_hash"] != plan or prev["code_fingerprint"] != fingerprint:
            raise LockboxError(f"the older history was already consumed under plan {prev['plan_hash']} / code {prev['code_fingerprint']}; "
                               f"this run is {plan} / {fingerprint}. It is no longer unseen evidence")
        prev["attempts"] = prev.get("attempts", 1) + 1
        marker.write_text(json.dumps(prev, indent=1, sort_keys=True))
        return prev
    rec = {"plan_hash": plan, "code_fingerprint": fingerprint, "first_run_at": datetime.now(timezone.utc).isoformat(), "attempts": 1}
    marker.write_text(json.dumps(rec, indent=1, sort_keys=True))
    return rec


def new_ledger(path: Path) -> LookLedger:
    ledger = LookLedger(path)
    if not ledger.entries():
        for row in RETROSPECTIVE_LOOKS:
            ledger.record(**row)
    return ledger


# ------------------------------------------------------------------ a segment's raw material


@dataclass
class Segment:
    name: str
    symbols: list[str]
    start_ts: int
    end_ts: int
    trades: dict[str, dict[str, list[TradeRecord]]] = field(default_factory=dict)  # arm -> symbol -> trades
    digests: dict[str, dict[str, RunDigest]] = field(default_factory=dict)  # arm -> symbol -> digest
    cost_trades: dict[float, dict[str, dict[str, list[TradeRecord]]]] = field(default_factory=dict)  # multiplier -> arm -> symbol -> trades
    regime: cs.RegimeIndex | None = None
    event_days: set[int] = field(default_factory=set)
    data_fingerprint: str = ""

    def pooled(self, arm: str, mult: float = 1.0) -> list[TradeRecord]:
        src = self.trades[arm] if mult == 1.0 else self.cost_trades[mult][arm]
        return [t for ts in src.values() for t in ts if t.exit_reason != "end_of_data"]


def _real(ts):
    return [t for t in ts if t.exit_reason != "end_of_data"]


def mean_r(ts) -> float | None:
    r = [t.r_net for t in _real(ts)]
    return statistics.fmean(r) if r else None


# ------------------------------------------------------------------ evaluation (pure: works on Segment objects)


def _account_metrics(digs: dict[str, RunDigest]) -> dict:
    per = {}
    for sym, d in digs.items():
        eq, curve = d.start_equity, []
        for i, r in enumerate(d.daily):
            eq *= 1 + r
            curve.append((i, eq))
        ret = (d.end_equity / d.start_equity - 1.0) * 100 if d.start_equity else None
        per[sym] = {"return_pct": ret, "max_dd_pct": d.max_dd_pct, "ulcer": cs.ulcer_index(curve),
                    "calmar": (ret / d.max_dd_pct) if (ret is not None and d.max_dd_pct and d.max_dd_pct > 0) else None}

    def avg(k):
        v = [p[k] for p in per.values() if p[k] is not None]
        return statistics.fmean(v) if v else None
    return {"per_pair": per, "mean_return_pct": avg("return_pct"), "mean_max_dd_pct": avg("max_dd_pct"), "mean_ulcer": avg("ulcer"), "mean_calmar": avg("calmar")}


def _boot(cand, base, c: Criteria) -> dict | None:
    return cs.paired_block_bootstrap(cand, base, block_days=c.block_days, iterations=c.bootstrap_iterations, seed=c.bootstrap_seed)


_ROUND = re.compile(r"-?\d+\.\d{5,}")


def _delta(cand, base) -> float | None:
    a, b = mean_r(cand), mean_r(base)
    return None if a is None or b is None else a - b


def evaluate_primary(arm: Arm, A: Segment, B: Segment | None, C: Segment | None, ref: dict | None, c: Criteria) -> dict:
    """Everything needed to judge one filter arm against the baseline. Holm adjustment is applied afterwards (family-wide)."""
    base, cand = A.pooled("BASE"), A.pooled(arm.id)
    out: dict = {"id": arm.id, "label": arm.label, "hypothesis": arm.hypothesis, "n_candidate": len(cand), "n_baseline": len(base)}
    out["boot"] = _boot(cand, base, c)
    out["delta_r"] = _delta(cand, base)
    out["candidate_r"], out["baseline_r"] = mean_r(cand), mean_r(base)
    out["robust_candidate"], out["robust_baseline"] = cs.robust_summary(cand), cs.robust_summary(base)
    out["retention"] = cs.retention(base, cand)
    out["design_effect_baseline"] = cs.design_effect(base, block_days=c.block_days)
    out["per_pair"] = {s: {"delta_r": _delta(A.trades[arm.id].get(s, []), A.trades["BASE"].get(s, [])), "n_candidate": len(_real(A.trades[arm.id].get(s, []))),
                           "n_baseline": len(_real(A.trades["BASE"].get(s, [])))} for s in A.symbols}
    dd_c = {s: d.max_dd_pct for s, d in A.digests[arm.id].items()}
    dd_b = {s: d.max_dd_pct for s, d in A.digests["BASE"].items()}
    out["worst_dd_candidate"], out["worst_dd_baseline"] = (max(dd_c.values()) if dd_c else None), (max(dd_b.values()) if dd_b else None)
    if arm.id in ("V67", "SM10"):
        out["kept_removed"] = cs.matched_kept_removed(base, cand, block_days=c.block_days, seed=c.bootstrap_seed)
    # regimes (BTC trailing return at entry)
    regimes = {}
    if A.regime is not None:
        for name in ("bull", "bear", "range"):
            cb = [t for t in base if A.regime.at(t.entry_ts) == name]
            cc = [t for t in cand if A.regime.at(t.entry_ts) == name]
            regimes[name] = {"n_baseline": len(cb), "n_candidate": len(cc), "baseline_r": mean_r(cb), "candidate_r": mean_r(cc), "delta_r": _delta(cc, cb)}
    out["regimes"] = regimes
    # costs
    pts_c, pts_b, by_mult = [], [], {}
    for m in sorted(set(COST_POINTS) | {1.0}):
        cm = mean_r(A.pooled(arm.id, m)) if (m == 1.0 or m in A.cost_trades) else None
        bm = mean_r(A.pooled("BASE", m)) if (m == 1.0 or m in A.cost_trades) else None
        by_mult[str(m)] = {"candidate_r": cm, "baseline_r": bm, "delta_r": (cm - bm) if (cm is not None and bm is not None) else None}
        pts_c.append((m, cm))
        pts_b.append((m, bm))
    out["costs"] = by_mult
    out["break_even"] = {"candidate": cs.break_even_multiplier(pts_c), "baseline": cs.break_even_multiplier(pts_b)}
    # event days: drop trades that overlap the most extreme BTC days, from both arms
    cb = [t for t in base if not cs.overlaps_event(t, A.event_days)]
    cc = [t for t in cand if not cs.overlaps_event(t, A.event_days)]
    out["event_days_excluded"] = {"n_days": len(A.event_days), "n_baseline": len(cb), "n_candidate": len(cc), "delta_r": _delta(cc, cb), "boot": _boot(cc, cb, c) if cb and cc else None}
    # other segments
    for seg, key in ((B, "segment_B"), (C, "segment_C")):
        if seg is not None and arm.id in seg.trades:
            sb, sc = seg.pooled("BASE"), seg.pooled(arm.id)
            out[key] = {"n_baseline": len(sb), "n_candidate": len(sc), "baseline_r": mean_r(sb), "candidate_r": mean_r(sc), "delta_r": _delta(sc, sb),
                        "boot": _boot(sc, sb, c) if sb and sc else None,
                        "per_pair": {s: _delta(seg.trades[arm.id].get(s, []), seg.trades["BASE"].get(s, [])) for s in seg.symbols}}
    p1 = (ref or {}).get("deltas", {}).get(arm.phase1_id) if arm.phase1_id else None
    out["phase1_delta_r"] = p1.get("delta_r") if p1 else None
    return out


def judge_primary(res: dict, c: Criteria, holm_p: float | None) -> dict:
    """Apply K0-K10. Each value is True / False / None (not applicable or not measurable). The rule for the verdict is fixed:
    PASS needs every applicable criterion; FAIL if direction (K1), shrinkage (K3) or asset/time replication (K7) fails;
    otherwise INCONCLUSIVE."""
    k: dict[str, bool | None] = {}
    why: dict[str, str] = {}

    def put(name, ok, detail):
        k[name], why[name] = ok, _ROUND.sub(lambda m: f"{float(m.group()):.4f}", detail)

    d = res["delta_r"]
    put("K0 arm is itself profitable after costs (segment A)", None if res["candidate_r"] is None else res["candidate_r"] > 0, f"net expectancy {res['candidate_r']}")
    put("K1 improves expectancy (segment A)", None if d is None else d > 0, f"delta {d}")
    put("K2 improvement unlikely to be noise (Holm-adjusted)", None if holm_p is None else holm_p <= c.holm_alpha, f"adjusted one-sided p {holm_p}")
    p1 = res.get("phase1_delta_r")
    if p1 is None or p1 <= 0 or d is None:
        put("K3 keeps a fair share of the Phase 1 improvement", None, "no positive Phase 1 reference for this arm")
    else:
        put("K3 keeps a fair share of the Phase 1 improvement", d >= c.min_shrinkage * p1, f"delta {d:.4f} vs {c.min_shrinkage:.0%} of Phase 1 {p1:.4f}")
    r = res["retention"]
    bw, td = r.get("big_winner_ratio"), r.get("top_decile_ratio")
    put("K4 large winners preserved", None if bw is None or td is None else (bw >= c.min_big_winner_ratio and td >= c.min_top_decile_ratio), f"big-winner ratio {bw}, top-decile ratio {td}")
    dc, db = res["worst_dd_candidate"], res["worst_dd_baseline"]
    put("K5 worst drawdown not materially worse", None if dc is None or db is None else dc <= db * c.max_drawdown_ratio, f"{dc} vs baseline {db}")
    pos = sum(1 for v in res["per_pair"].values() if v["delta_r"] is not None and v["delta_r"] > 0)
    put("K6 beats the baseline on most pairs (segment A)", pos >= min(c.min_pairs_positive, len(res["per_pair"])) if res["per_pair"] else None, f"{pos} of {len(res['per_pair'])} pairs")
    sb = res.get("segment_B")
    sc_ = res.get("segment_C")
    parts, detail = [], []
    if sb and sb["delta_r"] is not None:
        parts.append(sb["delta_r"] > 0)
        detail.append(f"B {sb['delta_r']:.4f}")
    if sc_ and sc_["delta_r"] is not None and sc_["n_baseline"] >= c.min_c_baseline_trades:
        parts.append(sc_["delta_r"] > 0)
        detail.append(f"C {sc_['delta_r']:.4f}")
    elif sc_:
        detail.append(f"C not judged ({sc_['n_baseline']} baseline trades)")
    put("K7 replicates on the six added pairs (segments B, C)", all(parts) if parts else None, ", ".join(detail) or "not measured")
    counted = {n: v for n, v in res["regimes"].items() if v["n_baseline"] >= c.regime_min_trades and v["delta_r"] is not None}
    if len(counted) >= 2:
        okr = sum(1 for v in counted.values() if v["delta_r"] > 0) >= min(c.regime_min_positive, len(counted)) and all(v["delta_r"] > c.regime_floor_r for v in counted.values())
        put("K8 not carried by one market regime", okr, str({n: round(v["delta_r"], 4) for n, v in counted.items()}))
    else:
        put("K8 not carried by one market regime", None, "fewer than two regimes with enough baseline trades")
    cm = res["costs"].get(str(c.cost_check_multiplier), {}).get("delta_r")
    put(f"K9 still better at {c.cost_check_multiplier}x costs", None if cm is None else cm > 0, f"delta {cm}")
    ev = res["event_days_excluded"]["delta_r"]
    put("K10 still better without the extreme BTC days", None if ev is None else ev > 0, f"delta {ev}")
    applicable = [v for v in k.values() if v is not None]
    failed = [n for n, v in k.items() if v is False]
    hard = [n for n in failed if n.startswith(("K1 ", "K3 ", "K7 "))]
    verdict = "PASS" if applicable and not failed else ("FAIL" if hard else "INCONCLUSIVE")
    return {"criteria": k, "detail": why, "failed": failed, "verdict": verdict}


def evaluate_sizing(arm: Arm, A: Segment, B: Segment | None, c: Criteria) -> dict:
    """Shorts-at-half-risk changes sizing, not entries, so R per trade is the wrong yardstick. Judged at account level."""
    def seg_metrics(seg):
        b, a = _account_metrics(seg.digests["BASE"]), _account_metrics(seg.digests[arm.id])
        ret_loss = None
        if b["mean_return_pct"] and b["mean_return_pct"] > 0 and a["mean_return_pct"] is not None:
            ret_loss = 1.0 - a["mean_return_pct"] / b["mean_return_pct"]
        shorts_b = [t for t in seg.pooled("BASE") if t.side == "short"]
        shorts_a = [t for t in seg.pooled(arm.id) if t.side == "short"]
        return {"baseline": b, "arm": a, "return_loss_share": ret_loss, "n_short_baseline": len(shorts_b), "n_short_arm": len(shorts_a),
                "short_mean_r_baseline": mean_r(shorts_b), "short_mean_r_arm": mean_r(shorts_a)}
    out = {"id": arm.id, "label": arm.label, "hypothesis": arm.hypothesis, "segment_A": seg_metrics(A)}
    if B is not None and arm.id in B.digests:
        out["segment_B"] = seg_metrics(B)
    k, why = {}, {}

    def judge(m, tag):
        b, a = m["baseline"], m["arm"]
        k[f"S1 risk-adjusted return not lower ({tag})"] = None if a["mean_calmar"] is None or b["mean_calmar"] is None else a["mean_calmar"] >= b["mean_calmar"]
        why[f"S1 risk-adjusted return not lower ({tag})"] = f"Calmar {a['mean_calmar']} vs {b['mean_calmar']}"
        k[f"S2 deeper-and-longer drawdowns reduced ({tag})"] = None if a["mean_ulcer"] is None or b["mean_ulcer"] is None else a["mean_ulcer"] < b["mean_ulcer"]
        why[f"S2 deeper-and-longer drawdowns reduced ({tag})"] = f"ulcer {a['mean_ulcer']} vs {b['mean_ulcer']}"
        rl = m["return_loss_share"]
        k[f"S3 gives up a limited share of the return ({tag})"] = None if rl is None else rl <= c.sizing_max_return_loss
        why[f"S3 gives up a limited share of the return ({tag})"] = f"return given up {rl}"
    judge(out["segment_A"], "segment A")
    if "segment_B" in out:
        judge(out["segment_B"], "segment B")
    failed = [n for n, v in k.items() if v is False]
    applicable = [v for v in k.values() if v is not None]
    out.update({"criteria": k, "detail": why, "failed": failed, "verdict": "PASS" if applicable and not failed else ("FAIL" if failed else "INCONCLUSIVE")})
    return out


def evaluate_context(arm: Arm, A: Segment, c: Criteria) -> dict:
    """Sensitivity and diagnostic arms: reported, never judged."""
    base, cand = A.pooled("BASE"), A.pooled(arm.id)
    return {"id": arm.id, "label": arm.label, "role": arm.role, "n_candidate": len(cand), "n_baseline": len(base), "candidate_r": mean_r(cand), "baseline_r": mean_r(base),
            "delta_r": _delta(cand, base), "boot": _boot(cand, base, c) if cand and base else None, "retention": cs.retention(base, cand),
            "by_side": {side: {"n": len([t for t in cand if t.side == side]), "mean_r": mean_r([t for t in cand if t.side == side])} for side in ("long", "short")}}


# ------------------------------------------------------------------ Auto Select studies


def candidate_set_frequencies(decisions: list[dict]) -> dict:
    sets: dict[str, int] = {}
    for d in decisions:
        key = "+".join(sorted(d["candidates"]))
        sets[key] = sets.get(key, 0) + 1
    return dict(sorted(sets.items(), key=lambda kv: -kv[1]))


def oracle_study(decisions: list[dict], fixed: dict[str, list[TradeRecord]]) -> dict:
    """At each Auto decision with two or more signalling pairs: what each pair's trade WOULD have returned, taken from that
    pair's own single-pair run (R is risk-normalised, so account size does not matter). Compares the pair Auto chose with
    the average of the candidates (what a random choice earns) and with the best of them (the unattainable upper bound)."""
    index = {(t.symbol, t.entry_ts): t.r_net for ts in fixed.values() for t in ts if t.exit_reason != "end_of_data"}
    resolved, chosen_r, avg_r, best_r, worst_r = 0, [], [], [], []
    for d in decisions:
        if d.get("entered") is False:
            continue
        rs = [index.get((s, d["ts"])) for s in d["candidates"]]
        if any(r is None for r in rs) or index.get((d["chosen"], d["ts"])) is None:
            continue
        resolved += 1
        chosen_r.append(index[(d["chosen"], d["ts"])])
        avg_r.append(statistics.fmean(rs))
        best_r.append(max(rs))
        worst_r.append(min(rs))
    out = {"decisions_logged": len(decisions), "decisions_resolved": resolved}
    if resolved:
        ch, av, be = statistics.fmean(chosen_r), statistics.fmean(avg_r), statistics.fmean(best_r)
        out.update({"chosen_mean_r": ch, "random_expected_mean_r": av, "oracle_mean_r": be, "worst_mean_r": statistics.fmean(worst_r),
                    "headroom_r": be - av, "captured_share": ((ch - av) / (be - av)) if be > av else None})
    return out


def auto_summary(prod: RunDigest, randoms: list[RunDigest], fixed: dict[str, list[TradeRecord]], c: Criteria, counters: dict, decisions: list[dict]) -> dict:
    pm = mean_r(prod.counted())
    seeds = [mean_r(d.counted()) for d in randoms]
    seeds = [x for x in seeds if x is not None]
    seeds_sorted = sorted(seeds)
    thr = seeds_sorted[min(len(seeds_sorted) - 1, int(c.auto_percentile * len(seeds_sorted)))] if seeds_sorted else None
    pooled_random = [t for d in randoms for t in d.counted()]
    boot = _boot(prod.counted(), pooled_random, c) if prod.counted() and pooled_random else None
    total = sum(counters.get(f"decision_polls_{k}", 0) for k in ("1_candidate", "2_candidates", "3plus_candidates"))
    multi = counters.get("decision_polls_2_candidates", 0) + counters.get("decision_polls_3plus_candidates", 0)
    return {
        "production": {"n_trades": len(prod.counted()), "mean_r": pm, "return_pct": (prod.end_equity / prod.start_equity - 1) * 100, "max_dd_pct": prod.max_dd_pct},
        "random": {"seeds": len(randoms), "mean_r_by_seed": seeds, "mean_of_seeds": statistics.fmean(seeds) if seeds else None,
                   "sd_of_seeds": statistics.pstdev(seeds) if len(seeds) > 1 else None, "percentile_threshold": thr,
                   "mean_return_pct": statistics.fmean((d.end_equity / d.start_equity - 1) * 100 for d in randoms) if randoms else None,
                   "mean_max_dd_pct": statistics.fmean(d.max_dd_pct for d in randoms) if randoms else None},
        "production_rank_among_random": (sum(1 for s in seeds if s < pm) / len(seeds)) if (seeds and pm is not None) else None,
        "paired_vs_pooled_random": boot,
        "ranking_adds_value": None if pm is None or thr is None else pm > thr,
        "simultaneous_signals": {"decision_polls": total, "with_two_or_more": multi, "share": (multi / total) if total else None,
                                 "counters": {k: counters.get(k) for k in ("decision_polls_1_candidate", "decision_polls_2_candidates", "decision_polls_3plus_candidates")},
                                 "candidate_sets": candidate_set_frequencies(decisions)},
        "oracle": oracle_study(decisions, fixed),
    }


# ------------------------------------------------------------------ running

_FLOOR_BARS = 24 * 30  # a pair with less than a month of hourly history in the older store is skipped


def _store_pairs(root: Path, pairs: tuple[str, ...]) -> tuple[list[str], list[str]]:
    have, missing = [], []
    for p in pairs:
        s = datalib.symbol_for(p)
        if datalib.kline_path(root, "1h", s).exists() and datalib.kline_path(root, "5m", s).exists() and len(datalib.load_candles(root, s, "1h")) >= _FLOOR_BARS:
            have.append(s)
        else:
            missing.append(s)
    return have, missing


def _run_fixed(lab: Lab, name: str, symbols: list[str], arms: tuple[Arm, ...], start_ts: int, end_ts: int, mults: tuple[float, ...],
               progress: Callable[[str], None]) -> Segment:
    seg = Segment(name, symbols, start_ts, end_ts)
    for a in arms:
        seg.trades[a.id], seg.digests[a.id] = {}, {}
    for sym in symbols:
        progress(f"[{name}:{sym}] arms")
        lab.load(sym)
        for a in arms:
            d = lab.run([sym], lab.config(a.policy, MODE_REPLAY, start_ts=start_ts, end_ts=end_ts))
            seg.trades[a.id][sym], seg.digests[a.id][sym] = d.counted(), d
        for m in mults:
            if m == 1.0:
                continue
            bucket = seg.cost_trades.setdefault(m, {})
            for a in arms:
                if a.id in COST_ARMS:
                    bucket.setdefault(a.id, {})[sym] = lab.run([sym], lab.config(a.policy, MODE_REPLAY, multiplier=m, start_ts=start_ts, end_ts=end_ts)).counted()
        lab.drop([sym])
    return seg


def _market_context(root: Path, seg: Segment, c: Criteria) -> None:
    btc = datalib.load_candles(root, "BTCUSDT", "1h") if datalib.kline_path(root, "1h", "BTCUSDT").exists() else []
    btc = [x for x in btc if seg.start_ts <= x.timestamp < seg.end_ts]
    if btc:
        seg.regime = cs.RegimeIndex(btc)
        seg.event_days = cs.event_days(btc, c.event_quantile)


def run_phase2(old_root: Path, phase1_root: Path, phase1_results: Path, out_dir: Path, options: Phase2Options | None = None, *,
               expect_plan_hash: str | None, expect_code_fingerprint: str | None, expect_phase1_sha256: str | None,
               progress: Callable[[str], None] = lambda m: None, registered: bool = True) -> dict:
    options = options or Phase2Options()
    c = options.criteria
    old_root, phase1_root, out_dir = Path(old_root), Path(phase1_root), Path(out_dir)
    plan, fp = plan_hash(options), code_fingerprint()
    if registered:
        if not (expect_plan_hash and expect_code_fingerprint and expect_phase1_sha256):
            raise RuntimeError("--expect-plan-hash, --expect-code-fingerprint and --expect-phase1-sha256 are all required")
        if plan != expect_plan_hash:
            raise RuntimeError(f"plan hash {plan} differs from the registered {expect_plan_hash}")
        if fp != expect_code_fingerprint:
            raise RuntimeError(f"code fingerprint {fp} differs from the registered {expect_code_fingerprint}")
    ref = load_phase1_reference(phase1_results, expect_sha256=expect_phase1_sha256 if registered else None)
    if synthetic.is_synthetic(old_root) != synthetic.is_synthetic(phase1_root):
        raise RuntimeError("one store is synthetic and the other is not")
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = new_ledger(out_dir / "look_ledger.jsonl")
    claim = claim_older_store(old_root, plan=plan, fingerprint=fp)

    p1_first = ref["span"][0]
    dev_end = ref["development_range"][1]
    lockbox_start = ref["lockbox_range"][0]
    if dev_end > lockbox_start:
        raise RuntimeError("the Phase 1 development range overlaps the lockbox: refusing")
    a_syms, a_missing = _store_pairs(old_root, options.a_pairs)
    c_syms, c_missing = _store_pairs(old_root, options.c_pairs)
    b_syms, b_missing = _store_pairs(phase1_root, options.b_pairs)
    if len(a_syms) < 2:
        raise RuntimeError("the older store needs 1h and 5-minute data for at least two of the four development pairs")
    # Data-quality gate: looks at the shape of the data (gaps, duplicates, impossible candles), never at returns.
    quality_report = quality.check_store(old_root, pairs=[datalib.pair_for(s) for s in dict.fromkeys(a_syms + c_syms)], intervals=("1h", "5m"), min_years=0.0)
    ledger.record(dataset="older_history", pairs=a_syms + c_syms, period="whole store", kind="descriptive", plan_hash=plan, purpose="data-quality gate only (gaps, duplicates, impossible candles); no returns examined")
    bad = [f["path"] for f in quality_report["files"] if f["status"] == "fail" and any(sym in f["path"] for sym in a_syms)]
    if bad:
        raise RuntimeError("the older store failed the data-quality gate for development pairs: " + ", ".join(bad) + ". Repair the download (same range) and re-run; nothing was analysed")

    lab_old = Lab(old_root, out_dir / "older", Phase1Options(account=options.account, costs=options.costs, strategy=options.strategy), progress)
    lab_p1 = Lab(phase1_root, out_dir / "phase1_store", Phase1Options(account=options.account, costs=options.costs, strategy=options.strategy), progress)
    spans_a = {s: lab_old.span(s) for s in a_syms}
    a_start = min(a for a, _ in spans_a.values())
    a_end = min(min(b for _, b in spans_a.values()), p1_first)  # never reach into the Phase 1 store
    ledger.record(dataset="older_history", pairs=a_syms + c_syms, period=f"{a_start}..{a_end} (ms)", kind="confirmation", plan_hash=plan,
                  purpose=f"Phase 2 one-shot confirmation (attempt {claim['attempts']})", note=f"code {fp}")
    ledger.record(dataset="phase1_store_extra_pairs", pairs=b_syms, period=f"{p1_first}..{dev_end} (ms)", kind="confirmation", plan_hash=plan,
                  purpose="asset out-of-sample confirmation of the frozen arms; baseline/descriptive results on these pairs were already seen in Phase 0/1, so they are not clean holdouts for these hypotheses afterwards")

    mults = tuple(options.cost_points)
    A = _run_fixed(lab_old, "A", a_syms, options.arms, a_start, a_end, mults, progress)
    A.data_fingerprint = data_hash(old_root)
    _market_context(old_root, A, c)
    primary_arms = tuple(a for a in options.arms if a.role in ("baseline", "primary", "sizing"))
    B = Cseg = None
    if b_syms:
        B = _run_fixed(lab_p1, "B", b_syms, primary_arms, p1_first, dev_end, (), progress)
        B.data_fingerprint = data_hash(phase1_root)
    if c_syms:
        c_end = min(min(lab_old.span(s)[1] for s in c_syms), p1_first)
        c_start = min(lab_old.span(s)[0] for s in c_syms)
        Cseg = _run_fixed(lab_old, "C", c_syms, primary_arms, c_start, c_end, (), progress)
        Cseg.data_fingerprint = A.data_fingerprint

    results: dict = {
        "meta": {"plan_hash": plan, "code_fingerprint": fp, "code_version": code_version(), "protocol": PROTOCOL_VERSION, "registered": registered,
                 "generated_at": datetime.now(timezone.utc).isoformat(), "synthetic": synthetic.is_synthetic(old_root),
                 "older_store_fingerprint": A.data_fingerprint, "phase1_store_fingerprint": data_hash(phase1_root),
                 "phase1_results_sha256": ref["sha256"], "phase1_plan_hash": ref["plan_hash"], "attempt": claim["attempts"],
                 "segments": {"A": {"pairs": a_syms, "missing": a_missing, "range": [a_start, a_end]},
                              "B": {"pairs": b_syms, "missing": b_missing, "range": [p1_first, dev_end]},
                              "C": {"pairs": c_syms, "missing": c_missing, "range": [Cseg.start_ts, Cseg.end_ts] if Cseg else None}},
                 "lockbox_untouched": True, "criteria": asdict(c), "n_event_days": len(A.event_days), "quality_summary": quality_report["summary"]},
        "phase1_reference": {k: ref[k] for k in ("sha256", "plan_hash", "data_fingerprint", "span", "development_range", "lockbox_range", "n_trials")},
        "primary": {}, "sizing": {}, "context": {}, "auto": None,
    }
    prim = {a.id: evaluate_primary(a, A, B, Cseg, ref, c) for a in options.arms if a.role == "primary"}
    pvals = {k: v["boot"]["p_one_sided"] for k, v in prim.items() if v.get("boot")}
    holm = cs.holm_adjust(pvals)
    for k, v in prim.items():
        v["holm_p"] = holm.get(k)
        v["judgement"] = judge_primary(v, c, holm.get(k))
        results["primary"][k] = v
    for a in options.arms:
        if a.role == "sizing":
            results["sizing"][a.id] = evaluate_sizing(a, A, B, c)
        elif a.role in ("sensitivity", "diagnostic"):
            results["context"][a.id] = evaluate_context(a, A, c)
    results["baseline"] = {"segment_A": {"stats": _brief(trade_stats(A.pooled("BASE"))), "robust": cs.robust_summary(A.pooled("BASE")),
                                         "design_effect": cs.design_effect(A.pooled("BASE"), block_days=c.block_days),
                                         "by_regime": {n: {"n": len([t for t in A.pooled("BASE") if A.regime and A.regime.at(t.entry_ts) == n]),
                                                           "mean_r": mean_r([t for t in A.pooled("BASE") if A.regime and A.regime.at(t.entry_ts) == n])} for n in ("bull", "bear", "range", "unknown")},
                                         "account": _account_metrics(A.digests["BASE"]), "per_pair": {s: _brief(trade_stats(A.trades["BASE"][s])) for s in a_syms}}}
    if B:
        results["baseline"]["segment_B"] = {"stats": _brief(trade_stats(B.pooled("BASE"))), "per_pair": {s: _brief(trade_stats(B.trades["BASE"][s])) for s in b_syms}}
    if Cseg:
        results["baseline"]["segment_C"] = {"stats": _brief(trade_stats(Cseg.pooled("BASE"))), "per_pair": {s: _brief(trade_stats(Cseg.trades["BASE"][s])) for s in c_syms}}

    # Auto Select (the four development pairs together, older history)
    if len(a_syms) >= 2:
        progress("[AUTO] production ranking vs random")
        for s in a_syms:
            lab_old.load(s)
        prod = lab_old.run_with_decisions(a_syms, lab_old.config(Policy(), MODE_REPLAY, auto=True, start_ts=a_start, end_ts=a_end))
        randoms = [lab_old.run(a_syms, lab_old.config(Policy(auto_score="random", auto_seed=sd), MODE_REPLAY, auto=True, start_ts=a_start, end_ts=a_end)) for sd in options.seeds]
        lab_old.drop(a_syms)
        results["auto"] = auto_summary(prod[0], randoms, A.trades["BASE"], c, prod[0].counters, prod[1])

    results["verdicts"] = {**{k: v["judgement"]["verdict"] for k, v in results["primary"].items()}, **{k: v["verdict"] for k, v in results["sizing"].items()}}
    results["ledger"] = ledger.entries()
    (out_dir / "phase2_results.json").write_text(json.dumps(results, indent=1, sort_keys=True, default=str))
    _write_trades_csv(out_dir / "phase2_trades.csv", {"A": A, "B": B, "C": Cseg})
    from .phase2_report import render

    (out_dir / "phase2_report.md").write_text(render(results))
    return results


def _brief(s: dict) -> dict:
    return {k: s.get(k) for k in ("n_trades", "win_rate", "expectancy_r", "expectancy_r_gross", "profit_factor", "avg_cost_r", "net_pnl", "best_r")}


def _write_trades_csv(path: Path, segs: dict[str, Segment | None]) -> None:
    lines = ["segment,arm,symbol,side,entry_ts,exit_ts,exit_reason,r_net"]
    for name, seg in segs.items():
        if seg is None:
            continue
        for arm, by_sym in seg.trades.items():
            for sym, ts in by_sym.items():
                lines += [f"{name},{arm},{sym},{t.side},{t.entry_ts},{t.exit_ts},{t.exit_reason},{t.r_net:.6f}" for t in ts]
    path.write_text("\n".join(lines) + "\n")
