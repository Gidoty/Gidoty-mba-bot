"""Phase 3: the strategy guards, MEASURED (research only; nothing here is imported by the trading service).

Question: do the downside guards in `strategy_guard_rules.py` (the SAME pure file the live path uses, pinned by hash below) reduce the
downside of the EMA 20/50 + ATR strategy, and what do they cost? Every claim in the weakness table of docs/safety/strategy_guards.md that
says "backtest effect" is filled from this runner's output, and says NOT MEASURED until it has been run on real data.

How it stays honest (pre-registration in docs/research/guards_preregistration.md, hash written before any result):
  * TUNE on the first 4 months of the last 6 only; the last 2 months are SEALED (validation.Lockbox, 3-day embargo) and are looked at ONCE
    (validation.LookLedger refuses a second confirmation look; a forced second look is labelled contaminated).
  * One grid per guard, fixed in advance. A value is eligible only under the registered rule (below). Nothing else is tried.
  * The guards are applied by a SUBCLASS of the production-replay engine swapped in for the duration of a run. engine.py, tape.py and
    every other registered or fingerprinted file stay byte-identical, so the Phase 1, Phase 2 and Design 4 registrations still hold.
  * Defaults reproduce the plain engine exactly (tested): the baseline arm never touches the subclass.
KNOWN DIFFERENCES from the live guards (written into every report): the live guards model net results as recorded gross pnl minus a flat
fee; here the engine's own net (fees, slippage, funding) is used. The expectancy pause never ends in a run (no owner release is simulated).
Each pair is run as its own account. The portfolio cap is not simulated (one position per run by construction). The fee guard's costs
are the registered assumptions, not measurements.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import strategy_guard_rules as R
from . import engine as eng
from .engine import AccountParams, BacktestConfig, CostModel, StrategyParams
from .metrics import trade_stats
from .phase1 import ORIGINAL_FOUR, Lab, Phase1Options, RunDigest
from .reporting import data_hash
from .validation import Lockbox, LockboxError, LookLedger, TrialRegistry, config_hash

PROTOCOL = "phase3-guards-v1"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
FAMILY = "phase3_guards"


def rules_sha256() -> str:
    return hashlib.sha256(Path(R.__file__).read_bytes()).hexdigest()


# ------------------------------------------------------------------ the guards as parameters


@dataclass(frozen=True)
class GuardParams:
    """Every field defaults to OFF (0). The names and meanings are those of the live settings (config.py)."""

    ema_min_gap_atr: float = 0.0
    cooldown_hours: float = 0.0
    fee_margin: float = 0.0
    fee_bps: float = 5.0
    slippage_bps: float = 11.0
    expected_move_atr: float = 1.0
    daily_loss_pct: float = 0.0
    pause_after_losses: int = 0
    streak_pause_hours: float = 24.0
    expectancy_window: int = 0

    def is_off(self) -> bool:
        return (self.ema_min_gap_atr <= 0 and self.cooldown_hours <= 0 and self.fee_margin <= 0 and self.daily_loss_pct <= 0
                and self.pause_after_losses <= 0 and self.expectancy_window <= 0)


@dataclass(frozen=True)
class Arm:
    name: str
    family: str  # gap | cooldown | fee | daily | streak | expectancy | base | combined
    guards: GuardParams
    description: str = ""


def _arms() -> tuple[Arm, ...]:
    out = [Arm("BASE", "base", GuardParams(), "the production strategy, no guard")]
    out += [Arm(f"GAP_{v:g}", "gap", GuardParams(ema_min_gap_atr=v), f"EMA gap at least {v:g} ATR") for v in (0.05, 0.10, 0.20, 0.30)]
    out += [Arm(f"COOL_{v:g}H", "cooldown", GuardParams(cooldown_hours=v), f"{v:g} hours after a stop loss on the pair") for v in (4.0, 12.0, 24.0)]
    out += [Arm(f"FEE_{v:g}", "fee", GuardParams(fee_margin=v), f"expected move at least {v:g} x round-trip cost") for v in (2.0, 3.0, 5.0)]
    out += [Arm(f"DAY_{v:g}", "daily", GuardParams(daily_loss_pct=v), f"stop new entries for the UTC day after a {v:g} percent loss") for v in (2.0, 3.0, 5.0)]
    out += [Arm(f"STREAK_{n}", "streak", GuardParams(pause_after_losses=n), f"pause 24 hours after {n} consecutive net losses") for n in (3, 4, 5)]
    out += [Arm("EXPECT_20", "expectancy", GuardParams(expectancy_window=20), "pause (never resumes) if net expectancy over the last 20 trades is negative")]
    return tuple(out)


ARMS = _arms()
FAMILIES = ("gap", "cooldown", "fee", "daily", "streak", "expectancy")


@dataclass(frozen=True)
class Rule:
    """The registered eligibility rule: a guard value carries to the sealed window only if, on the tune window, it (a) keeps at least this
    share of the baseline's trades, (b) loses no more than this much net expectancy per trade, and (c) does not worsen the maximum
    drawdown. Among eligible values the lowest drawdown wins, then the higher expectancy. Nothing else is tried."""

    min_trade_share: float = 0.5
    max_expectancy_drop_r: float = 0.02
    sealed_stress_multiplier: float = 1.5


@dataclass(frozen=True)
class Phase3Options:
    dev_pairs: tuple[str, ...] = ORIGINAL_FOUR
    total_months: float = 6.0
    sealed_months: float = 2.0
    embargo_days: float = 3.0
    mode: str = eng.MODE_REPLAY
    account: AccountParams = field(default_factory=lambda: AccountParams(allocated_capital=1000.0))
    costs: CostModel = field(default_factory=CostModel)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    rule: Rule = field(default_factory=Rule)
    arms: tuple[Arm, ...] = ARMS


def plan_hash(options: Phase3Options | None = None) -> str:
    o = options or Phase3Options()
    blob = {"arms": [asdict(a) for a in o.arms], "pairs": o.dev_pairs, "months": [o.total_months, o.sealed_months, o.embargo_days], "mode": o.mode,
            "account": asdict(o.account), "costs": asdict(o.costs), "strategy": asdict(o.strategy), "rule": asdict(o.rule), "protocol": PROTOCOL,
            "rules_sha256": rules_sha256()}
    return hashlib.sha1(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ the guarded engine (a subclass; engine.py is untouched)

_ACTIVE: GuardParams | None = None


class GuardedEngine(eng._Engine):
    """The production-replay engine with the guards in front of every entry attempt. Same order of checks as `strategy_guards.evaluate`:
    an existing pause, daily loss, cooldown, loss streak, expectancy, fee guard, EMA gap. The first hit refuses the entry."""

    def __init__(self, pairs, config, aux):
        super().__init__(pairs, config, aux)
        self.gp: GuardParams = _ACTIVE or GuardParams()
        self.pauses: list[dict] = []  # {"code", "start", "end" (ms or None = never)}
        for code in R.ALL_CODES:
            self.counters["guard_rejections_" + code] = 0
        self.counters["guard_pauses_" + R.R_LOSS_STREAK] = 0
        self.counters["guard_pauses_" + R.R_EXPECTANCY] = 0
        self.counters["guard_expectancy_pause_start_ts"] = 0
        self.counters["guard_streak_paused_hours"] = 0

    # -- helpers
    def _dt(self, ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    def _window_start(self, code: str, ts: int) -> int | None:
        ends = [p["end"] for p in self.pauses if p["code"] == code and p["end"] is not None and p["end"] <= ts]
        return max(ends) if ends else None

    def _results(self, since: int | None) -> list:
        rows = [t for t in self.trades if t.exit_reason != "end_of_data" and (since is None or t.exit_ts > since)]
        return sorted(rows, key=lambda t: t.exit_ts, reverse=True)  # newest first

    def guard_hits(self, sym: str, sig, ts: int) -> list[R.GuardHit]:
        gp, now, hits = self.gp, self._dt(ts), []
        active = [p for p in self.pauses if p["end"] is None or p["end"] > ts]
        if active:
            hits.append(R.GuardHit(R.R_PAUSED, {"pause_code": active[0]["code"]}))
        if gp.daily_loss_pct > 0:
            day_start = ts - ts % DAY_MS
            net_today = sum(t.net_pnl for t in self.trades if t.exit_reason != "end_of_data" and t.exit_ts >= day_start)
            h = R.daily_loss_hit(net_pnl_today=net_today, capital=self.cfg.account.allocated_capital, limit_pct=gp.daily_loss_pct)
            if h:
                hits.append(h)
        if gp.cooldown_hours > 0:
            stops = [t.exit_ts for t in self.trades if t.symbol == sym and t.exit_reason == R.STOP_REASON]
            h = R.cooldown_hit(self._dt(max(stops)) if stops else None, now, gp.cooldown_hours)
            if h:
                hits.append(h)
        if gp.pause_after_losses > 0:
            res = self._results(self._window_start(R.R_LOSS_STREAK, ts))
            h = R.loss_streak_hit([t.net_pnl for t in res], gp.pause_after_losses, gp.streak_pause_hours)
            if h:
                hits.append(h)
        if gp.expectancy_window > 0:
            res = self._results(self._window_start(R.R_EXPECTANCY, ts))
            h = R.expectancy_hit([t.r_net for t in res], gp.expectancy_window)
            if h:
                hits.append(h)
        h = R.fee_guard_hit(atr_pct=getattr(sig, "atr_pct", None), margin=gp.fee_margin, fee_bps=gp.fee_bps, slippage_bps=gp.slippage_bps,
                            expected_move_atr=gp.expected_move_atr)
        if h:
            hits.append(h)
        gap = abs(sig.fast_ema - sig.slow_ema) / sig.atr if (sig.atr and sig.fast_ema is not None and sig.slow_ema is not None) else None
        h = R.ema_gap_hit(gap, gp.ema_min_gap_atr)
        if h:
            hits.append(h)
        return hits

    def _try_enter(self, sym, sig, price, ts, window_fn, *, risk_scale=1.0, extra=None):
        if self.pos is None and self._side_for(sig) is not None and sig.atr:
            hits = self.guard_hits(sym, sig, ts)
            if hits:
                hit = hits[0]
                self.counters["guard_rejections_" + hit.code] += 1
                if hit.creates_pause and hit.code != R.R_PAUSED:
                    end = ts + int(hit.pause_hours * HOUR_MS) if hit.pause_hours else None
                    self.pauses.append({"code": hit.code, "start": ts, "end": end})
                    self.counters["guard_pauses_" + hit.code] += 1
                    if end is None:
                        self.counters["guard_expectancy_pause_start_ts"] = self.counters["guard_expectancy_pause_start_ts"] or ts
                    else:
                        self.counters["guard_streak_paused_hours"] += int(hit.pause_hours)
                return False
        return super()._try_enter(sym, sig, price, ts, window_fn, risk_scale=risk_scale, extra=extra)


@contextmanager
def guards_active(params: GuardParams):
    """Swap the guarded engine in for the duration of a run. With all guards off the plain engine is used untouched."""
    global _ACTIVE
    if params.is_off():
        yield
        return
    saved, saved_params = eng._Engine, _ACTIVE
    eng._Engine, _ACTIVE = GuardedEngine, params
    try:
        yield
    finally:
        eng._Engine, _ACTIVE = saved, saved_params


# ------------------------------------------------------------------ running arms


def _losing_streak(trades) -> int:
    best = cur = 0
    for t in sorted(trades, key=lambda t: t.exit_ts):
        cur = cur + 1 if t.net_pnl <= 0 else 0
        best = max(best, cur)
    return best


def pooled(digests: list[RunDigest]) -> dict:
    trades = [t for d in digests for t in d.counted()]
    s = trade_stats(trades)
    s["n_trades"] = len(trades)
    s["total_net_r"] = sum(t.r_net for t in trades)
    s["max_dd_pct"] = max((d.max_dd_pct for d in digests), default=0.0)
    s["worst_losing_streak"] = max((_losing_streak(d.counted()) for d in digests), default=0)
    g: dict[str, int] = {}
    for d in digests:
        for k, v in d.counters.items():
            if k.startswith("guard_"):
                g[k] = g.get(k, 0) + v
    s["guard_counters"] = g
    return s


def run_arm(lab: Lab, arm: Arm, options: Phase3Options, symbols: list[str], start: int, end: int, *, cost_multiplier: float = 1.0) -> dict:
    costs = replace(options.costs, multiplier=options.costs.multiplier * cost_multiplier)
    cfg = BacktestConfig(mode=options.mode, strategy=options.strategy, account=options.account, costs=costs, auto=False, allow_short=True,
                         start_ts=start, end_ts=end)
    with guards_active(arm.guards):
        digests = [lab.run([s], cfg) for s in symbols]
    return pooled(digests)


def lab_for(root: Path, out_dir: Path, options: Phase3Options, progress: Callable[[str], None]) -> Lab:
    o1 = Phase1Options(dev_pairs=options.dev_pairs, account=options.account, costs=options.costs, strategy=options.strategy)
    return Lab(root, out_dir, o1, progress)


def windows(lab: Lab, options: Phase3Options) -> Lockbox:
    spans = [lab.span(s) for s in options.dev_pairs]
    last = min(e for _, e in spans)
    first_needed = int(last - options.total_months * 30.4375 * DAY_MS)
    if first_needed < max(s for s, _ in spans):
        raise LockboxError(f"the store holds less than {options.total_months:g} months for these pairs")
    return Lockbox(first_ts=first_needed, last_ts=last, months=options.sealed_months, holdout_symbols=frozenset(), embargo_days=options.embargo_days)


# ------------------------------------------------------------------ selection (the registered rule)


def eligible(base: dict, row: dict, rule: Rule) -> tuple[bool, list[str]]:
    why = []
    bn = base.get("n_trades", 0)
    if bn and row.get("n_trades", 0) < rule.min_trade_share * bn:
        why.append(f"keeps {row.get('n_trades', 0)} of {bn} trades, under {rule.min_trade_share:.0%}")
    be, re_ = base.get("expectancy_r"), row.get("expectancy_r")
    if be is not None and (re_ is None or re_ < be - rule.max_expectancy_drop_r):
        why.append(f"expectancy {re_} against {be:.4f}, more than {rule.max_expectancy_drop_r} R worse")
    if row.get("max_dd_pct", 0.0) > base.get("max_dd_pct", 0.0) + 1e-12:
        why.append("max drawdown is worse")
    return (not why), why


def select(rows: dict[str, dict], arms: tuple[Arm, ...], rule: Rule) -> dict:
    """One value per guard family or none. Returns {family: {"arm": name, "reasons": ...}}."""
    base = rows["BASE"]
    out = {}
    for fam in FAMILIES:
        cands = []
        for a in arms:
            if a.family != fam:
                continue
            ok, why = eligible(base, rows[a.name], rule)
            cands.append((a, ok, why))
        elig = [a for a, ok, _ in cands if ok]
        if not elig:
            out[fam] = {"arm": None, "reasons": {a.name: why for a, _, why in cands}}
            continue
        best = sorted(elig, key=lambda a: (rows[a.name].get("max_dd_pct", 0.0), -(rows[a.name].get("expectancy_r") or -9)))[0]
        out[fam] = {"arm": best.name, "reasons": {}}
    return out


def combine(selected: dict, arms: tuple[Arm, ...]) -> GuardParams:
    by_name = {a.name: a for a in arms}
    kw: dict = {}
    for fam, sel in selected.items():
        if sel["arm"]:
            g = by_name[sel["arm"]].guards
            for f, v in asdict(g).items():
                if v != asdict(GuardParams())[f]:
                    kw[f] = v
    return GuardParams(**kw)


# ------------------------------------------------------------------ tune and confirm


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_tune(root: Path, out_dir: Path, options: Phase3Options | None = None, *, expect_plan_hash: str | None = None,
             progress: Callable[[str], None] = lambda m: None) -> dict:
    """Runs every registered arm on the TUNE window only (the first months of the six, up to the embargo before the sealed months)."""
    options = options or Phase3Options()
    ph = plan_hash(options)
    if expect_plan_hash and expect_plan_hash != ph:
        raise LockboxError(f"the plan differs from the registered one ({ph}, expected {expect_plan_hash}); refusing to run")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lab = lab_for(root, out_dir, options, progress)
    lb = windows(lab, options)
    t0, t1 = lb.develop_range()
    dh = data_hash(Path(root))
    registry, ledger = TrialRegistry(out_dir / "registry.jsonl"), LookLedger(out_dir / "looks.jsonl")
    ledger.record(dataset=f"guards_{dh}", pairs=list(options.dev_pairs), period=f"tune window {t0}..{t1}", kind="design",
                  purpose="Phase 3 guard tuning on the first months of the last six; the sealed months are not read", plan_hash=ph)
    rows: dict[str, dict] = {}
    for a in options.arms:
        progress(f"tune: {a.name}")
        rows[a.name] = run_arm(lab, a, options, list(options.dev_pairs), t0, t1)
        registry.record_trial(family=FAMILY, config={"arm": a.name, "guards": asdict(a.guards)}, data_hash=dh, window="tune",
                              summary={k: rows[a.name].get(k) for k in ("n_trades", "expectancy_r", "max_dd_pct", "total_net_r")})
    sel = select(rows, options.arms, options.rule)
    combo = combine(sel, options.arms)
    combo_arm = Arm("SELECTED", "combined", combo, "every selected guard together")
    progress("tune: SELECTED (all selected guards together)")
    rows["SELECTED"] = run_arm(lab, combo_arm, options, list(options.dev_pairs), t0, t1)
    result = {"protocol": PROTOCOL, "plan_hash": ph, "rules_sha256": rules_sha256(), "data_hash": dh, "created": _stamp(), "pairs": list(options.dev_pairs),
              "mode": options.mode, "tune_range": [t0, t1], "sealed_range": list(lb.lockbox_range()), "embargo_days": options.embargo_days,
              "rows": rows, "selected": sel, "combined": asdict(combo), "rule": asdict(options.rule)}
    (out_dir / "tune_results.json").write_text(json.dumps(result, indent=1, default=str))
    (out_dir / "selected.json").write_text(json.dumps({k: result[k] for k in ("protocol", "plan_hash", "rules_sha256", "data_hash", "pairs", "mode", "sealed_range",
                                                                               "tune_range", "selected", "combined")}, indent=1, default=str))
    (out_dir / "tune_report.md").write_text(tune_report(result))
    return result


def run_confirm(root: Path, out_dir: Path, options: Phase3Options | None = None, *, reason: str, confirm: bool, second_look: bool = False,
                progress: Callable[[str], None] = lambda m: None) -> dict:
    """The ONE look at the sealed months. Baseline against the selected guards (and each selected guard alone), with a cost stress."""
    options = options or Phase3Options()
    out_dir = Path(out_dir)
    sel_path = out_dir / "selected.json"
    if not sel_path.exists():
        raise LockboxError("run the tuning first: there is no selected.json")
    chosen = json.loads(sel_path.read_text())
    ph, dh = plan_hash(options), data_hash(Path(root))
    for key, now in (("plan_hash", ph), ("rules_sha256", rules_sha256()), ("data_hash", dh)):
        if chosen[key] != now:
            raise LockboxError(f"{key} differs from the one the tuning used ({chosen[key]} against {now}); refusing to open the sealed months")
    lab = lab_for(root, out_dir, options, progress)
    lb = windows(lab, options)
    if list(lb.lockbox_range()) != list(chosen["sealed_range"]):
        raise LockboxError("the sealed range changed since the tuning (the data store changed)")
    registry, ledger = TrialRegistry(out_dir / "registry.jsonl"), LookLedger(out_dir / "looks.jsonl")
    dataset = f"guards_{dh}"
    contaminated = False
    if ledger.confirmations(dataset):
        if not second_look:
            ledger.assert_unconsumed(dataset)  # raises LockboxError
        contaminated = True
    opened = lb.open(registry, family=FAMILY, reason=reason, confirm=confirm)
    ledger.record(dataset=dataset, pairs=list(options.dev_pairs), period=f"sealed window {lb.lockbox_range()}", kind="confirmation",
                  purpose="Phase 3 one-shot confirmation of the selected guards", plan_hash=ph, note="SECOND LOOK: contaminated" if contaminated else "")
    s0, s1 = lb.lockbox_range()
    combo = GuardParams(**chosen["combined"])
    arms = [Arm("BASE", "base", GuardParams()), Arm("SELECTED", "combined", combo)]
    by_name = {a.name: a for a in options.arms}
    for fam, sel in chosen["selected"].items():
        if sel["arm"]:
            arms.append(by_name[sel["arm"]])
    rows = {}
    for a in arms:
        progress(f"sealed: {a.name}")
        rows[a.name] = run_arm(lab, a, options, list(options.dev_pairs), s0, s1)
    mult = options.rule.sealed_stress_multiplier
    for name in ("BASE", "SELECTED"):
        rows[f"{name}@{mult:g}xcost"] = run_arm(lab, next(a for a in arms if a.name == name), options, list(options.dev_pairs), s0, s1, cost_multiplier=mult)
    result = {"protocol": PROTOCOL, "plan_hash": ph, "rules_sha256": rules_sha256(), "data_hash": dh, "created": _stamp(), "sealed_range": [s0, s1],
              "lockbox": opened, "contaminated": contaminated, "rows": rows, "selected": chosen["selected"], "combined": chosen["combined"]}
    tune = json.loads((out_dir / "tune_results.json").read_text())
    (out_dir / "sealed_results.json").write_text(json.dumps(result, indent=1, default=str))
    (out_dir / "final_report.md").write_text(final_report(tune, result, options))
    return result


# ------------------------------------------------------------------ reports


def _fmt(s: dict | None) -> str:
    if not s or not s.get("n_trades"):
        return "no trades"
    e = s.get("expectancy_r")
    return f"{s['n_trades']} trades, expectancy {e:+.3f} R, net {s['total_net_r']:+.1f} R, max drawdown {s['max_dd_pct']:.1f}%, worst losing streak {s['worst_losing_streak']}"


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def tune_report(res: dict) -> str:
    rows, base = res["rows"], res["rows"]["BASE"]
    t0, t1 = res["tune_range"]
    lines = [f"# Phase 3 guards: tuning window {_date(t0)} to {_date(t1)} (sealed months NOT read)", "",
             f"Plan {res['plan_hash']}, rules file SHA-256 {res['rules_sha256']}, data {res['data_hash']}, pairs {', '.join(res['pairs'])}, mode {res['mode']}.",
             "These numbers come from the tuning window only. They are the basis for choosing one value per guard; they are not evidence that a guard helps.", "",
             "| Arm | Trades | Expectancy (R) | Net R | Max DD % | Worst losing streak | Eligible |", "|---|---|---|---|---|---|---|"]
    arms = {a.name: a for a in ARMS}
    for name, r in rows.items():
        if not r.get("n_trades"):
            lines.append(f"| {name} | 0 | | | | | |")
            continue
        ok = "baseline" if name == "BASE" else ("combined" if name == "SELECTED" else ("yes" if eligible(base, r, Rule(**res["rule"]))[0] else "no"))
        lines.append(f"| {name} | {r['n_trades']} | {r['expectancy_r']:+.3f} | {r['total_net_r']:+.1f} | {r['max_dd_pct']:.1f} | {r['worst_losing_streak']} | {ok} |")
    lines += ["", "## Selected under the registered rule", ""]
    for fam, s in res["selected"].items():
        lines.append(f"* {fam}: " + (s["arm"] if s["arm"] else "none (no value was eligible)"))
    lines += ["", "## Guard activity on the tuning window", ""]
    for name in ("SELECTED",):
        for k, v in sorted(rows[name].get("guard_counters", {}).items()):
            if v:
                lines.append(f"* {name} {k}: {v}")
    return "\n".join(lines) + "\n"


ROWS = (
    ("ema_cross_atr_v1", "False crossovers in choppy markets", "gap"),
    ("ema_cross_atr_v1", "Repeated stop-outs in the same chop", "cooldown"),
    ("All", "Fee drag", "fee"),
    ("All", "Losing streaks (daily loss limit)", "daily"),
    ("All", "Losing streaks (pause after N losses)", "streak"),
    ("All", "A strategy that stops working", "expectancy"),
)


def effect_text(fam: str, tune: dict, sealed: dict | None) -> str:
    sel = tune["selected"].get(fam, {})
    if not sel.get("arm"):
        return f"tune window: no value was eligible under the registered rule (baseline {_fmt(tune['rows']['BASE'])})"
    t = f"tune ({_date(tune['tune_range'][0])} to {_date(tune['tune_range'][1])}), {sel['arm']}: {_fmt(tune['rows'][sel['arm']])} against baseline {_fmt(tune['rows']['BASE'])}"
    if sealed and sel["arm"] in sealed["rows"]:
        t += f". Sealed ({_date(sealed['sealed_range'][0])} to {_date(sealed['sealed_range'][1])}), {sel['arm']}: {_fmt(sealed['rows'][sel['arm']])} against baseline {_fmt(sealed['rows']['BASE'])}"
        if sealed.get("contaminated"):
            t += " (SECOND LOOK: contaminated)"
    return t


def final_report(tune: dict, sealed: dict, options: Phase3Options) -> str:
    lines = [f"# Phase 3 guards: final report (plan {sealed['plan_hash']})", "",
             "Tuned on the first months only, then ONE look at the sealed months. A two-month sealed window is short: it cannot show a lasting effect, "
             "and a guard that 'helps' here may be luck. Nothing below says a weakness is removed.", "",
             f"Combined selection on the sealed window: {_fmt(sealed['rows']['SELECTED'])}", f"Baseline on the sealed window: {_fmt(sealed['rows']['BASE'])}",
             f"Under a {options.rule.sealed_stress_multiplier:g}x cost stress: baseline {_fmt(sealed['rows'].get('BASE@%gxcost' % options.rule.sealed_stress_multiplier))}; "
             f"selected {_fmt(sealed['rows'].get('SELECTED@%gxcost' % options.rule.sealed_stress_multiplier))}", "",
             "| Strategy | Weakness | Backtest effect |", "|---|---|---|"]
    for strat, weak, fam in ROWS:
        lines.append(f"| {strat} | {weak} | {effect_text(fam, tune, sealed)} |")
    lines += ["| rsi_mr_v1, Donchian breakout | all | NOT MEASURED (the strategies do not exist) |", ""]
    if sealed.get("contaminated"):
        lines.append("WARNING: this was NOT the first look at the sealed months; the result is no longer unseen evidence.")
    return "\n".join(lines) + "\n"
