"""Markdown report on the trade ledger. Research only; reads nothing but the sanitised bundle.

Order of the report is deliberate: counts first (what the sample can and cannot support), then costs, then results, then the
incident trades on their own, and only then a verdict. The verdict is WITHHELD unless (a) the sample reaches the registered
minimum and (b) the owner has agreed the action rules in the registration file before this run.
"""
from __future__ import annotations

import hashlib
import re
import statistics
from pathlib import Path

from .cost_calibration import MIN_OBS_COST, build_cost_model, measure, validate_in_time
from .engine import CostModel
from .ledger import CLASS_INCIDENT, CLASS_NORMAL, CLASS_PAPER, Economics, counts, usable
from .metrics import bootstrap_mean_ci, wilson_interval

N_MIN_VERDICT = 30  # reconciled live NORMAL trades needed before any statement about edge

AGREED_RE = re.compile(r"^Owner agreement:\s*AGREED\b", re.M)
PENDING_RE = re.compile(r"^Owner agreement:\s*PENDING\b", re.M)


def prereg_status(path: str | Path | None) -> tuple[str, str | None]:
    """('AGREED'|'PENDING'|'MISSING'|'UNCLEAR', sha256 of the file). Anything but AGREED withholds the verdict."""
    if not path or not Path(path).is_file():
        return "MISSING", None
    text = Path(path).read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    agreed, pending = bool(AGREED_RE.search(text)), bool(PENDING_RE.search(text))
    return ("AGREED" if agreed and not pending else "PENDING" if pending and not agreed else "UNCLEAR"), sha


def verdict(r_values: list[float], status: str) -> dict:
    n = len(r_values)
    if n < N_MIN_VERDICT:
        return {"label": "INSUFFICIENT SAMPLE", "text": f"{n} reconciled live normal trades, below the registered minimum of {N_MIN_VERDICT}. Descriptive numbers only; no statement about edge is made."}
    if status != "AGREED":
        return {"label": "WITHHELD", "text": f"The owner's agreement on the action rules is {status} in the registration file, so no verdict is issued. The numbers above stand."}
    ci = bootstrap_mean_ci(r_values)
    if ci is None:
        return {"label": "INSUFFICIENT SAMPLE", "text": "the interval could not be computed"}
    if ci["low"] > 0:
        return {"label": "POSITIVE", "text": f"mean net R is positive and its 95% interval ({ci['low']:+.3f} to {ci['high']:+.3f}) is above zero. Trades are treated as independent here, which overstates certainty when trades cluster in time."}
    if ci["high"] < 0:
        return {"label": "NEGATIVE", "text": f"mean net R is negative and its 95% interval ({ci['low']:+.3f} to {ci['high']:+.3f}) is below zero."}
    return {"label": "NO EDGE DETECTED", "text": f"the 95% interval for mean net R ({ci['low']:+.3f} to {ci['high']:+.3f}) includes zero: this sample cannot tell a small edge from none."}


def _f(x, d=2, plus=False):
    if x is None:
        return "n/a"
    return f"{x:+.{d}f}" if plus else f"{x:.{d}f}"


def render(rows: list[Economics], meta: dict, *, prereg_path: str | None = None, base: CostModel | None = None) -> str:
    status, sha = prereg_status(prereg_path)
    c = counts(rows)
    normal = usable(rows, CLASS_NORMAL)
    incident = usable(rows, CLASS_INCIDENT)
    m = measure(rows)
    model, prov = build_cost_model(m, base)
    val = validate_in_time(rows)
    L = ["# Trade ledger report", "",
         f"Registration: {prereg_path or 'none given'} (sha256 {sha or 'n/a'}), owner agreement: {status}.",
         "Costs below are MEASURED from the exchange's own fills where the sample allows, and ASSUMED otherwise (labelled).", "",
         "## 1. What the sample is", "",
         f"| class | all | reconciled | unreconciled |", "|---|---|---|---|"]
    for k in (CLASS_NORMAL, CLASS_INCIDENT, CLASS_PAPER):
        L.append(f"| {k} | {c[k]['all']} | {c[k]['reconciled']} | {c[k]['unreconciled'] if k != CLASS_PAPER else 'n/a'} |")
    L += ["", "Unreconciled trades (fills that do not net to flat, a size mismatch, or an incomplete record) are excluded from every number below and are listed in the CSV.",
          f"Registered minimum for a statement about edge: {N_MIN_VERDICT} reconciled live normal trades. Here: {len(normal)}.", ""]
    L += ["## 2. Costs: measured versus the research model", "", "| item | model assumed | measured | observations | used in the re-run |", "|---|---|---|---|---|"]
    base = base or CostModel()
    fee, es, ss = m["taker_fee_bps"], m["entry_slip_bps_all"], m["stop_slip_bps_all"]
    L.append(f"| taker fee (bp) | {base.taker_fee_bps} | {_f(fee.get('mean'))} | {fee.get('n', 0)} | {prov['taker_fee_bps']} |")
    L.append(f"| market-order slippage (bp, adverse +) | {base.market_slip_bps} | {_f(es.get('mean'))} (median {_f(es.get('median'))}, p90 {_f(es.get('p90'))}) | {es.get('n', 0)} | {prov['market_slip_bps']} |")
    L.append(f"| stop-exit slippage (bp, adverse +) | {base.stop_slip_bps} | {_f(ss.get('mean'))} (median {_f(ss.get('median'))}, p90 {_f(ss.get('p90'))}) | {ss.get('n', 0)} | {prov['stop_slip_bps']} |")
    f8 = m["funding"]
    L.append(f"| funding paid (bp of notional per 8 h) | historical rates | {_f(f8.get('bps_per_8h'))} | {f8.get('n', 0)} | recorded rates stay in use |")
    L += ["", f"Maker share of legs: {_f(m['maker_leg_share'], 2)}. Trades whose commission was paid partly in another asset (not converted): {m['fees_in_other_assets_trades']}. "
              f"A measured value replaces an assumption only at {MIN_OBS_COST} or more observations.", ""]
    L += [f"Time-ordered check (fit on the earlier 60%, compare with the later 40%): **{val['status']}**" + (f" - {val['reason']}" if val.get("reason") else "") + "."]
    if val.get("drift"):
        L.append("Drift flagged for: " + ", ".join(val["drift"]) + ". Treat the measured costs as unstable.")
    L += ["", "## 3. Results, live normal trades (reconciled)", ""]
    if normal:
        r = [x.r_net for x in normal if x.r_net is not None]
        net = sum(x.net_pnl for x in normal)
        gross = sum(x.gross_pnl for x in normal)
        fees = sum(x.fees_usdt for x in normal)
        fund = sum(x.funding_usdt for x in normal)
        wins = sum(1 for x in normal if x.net_pnl > 0)
        wi = wilson_interval(wins, len(normal))
        ci = bootstrap_mean_ci(r) if r else None
        L += [f"- trades {len(normal)}; net {net:+.2f} USDT = gross {gross:+.2f} - fees {fees:.2f} + funding {fund:+.2f}",
              f"- mean net R {_f(statistics.fmean(r), 3, True) if r else 'n/a'}" + (f" (95% interval {ci['low']:+.3f} to {ci['high']:+.3f})" if ci else " (interval needs 5 or more trades)") + (f", median {_f(statistics.median(r), 3, True)}" if r else ""),
              f"- win rate {wins}/{len(normal)}" + (f" (95% interval {wi[0]:.0%} to {wi[1]:.0%})" if wi else ""),
              f"- costs ate {abs(fees - fund) / abs(gross) * 100:.0f}% of the gross result" if gross else "- gross result is zero"]
        by_pair = {}
        for x in normal:
            by_pair.setdefault(x.pair, []).append(x)
        L += ["", "| pair | trades | net USDT | mean net R |", "|---|---|---|---|"]
        for pair, xs in sorted(by_pair.items()):
            rs = [x.r_net for x in xs if x.r_net is not None]
            L.append(f"| {pair} | {len(xs)} | {sum(x.net_pnl for x in xs):+.2f} | {_f(statistics.fmean(rs), 3, True) if rs else 'n/a'} |")
        gap = [x.recorded_vs_fills_pnl_gap for x in normal if x.recorded_vs_fills_pnl_gap is not None]
        if gap:
            L += ["", f"Recorded `trades.pnl` minus gross-from-fills: mean {statistics.fmean(gap):+.4f} USDT over {len(gap)} trades (a mean near the average fee per trade suggests the recorded pnl is net of fees; this is a hint, not proof)."]
    else:
        L.append("No reconciled live normal trades.")
    L += ["", "## 4. Incident trades (open then emergency close), reported separately", ""]
    if incident:
        L += [f"- {len(incident)} reconciled; total net {sum(x.net_pnl for x in incident):+.2f} USDT, fees {sum(x.fees_usdt for x in incident):.2f} USDT, median hold {statistics.median(x.hold_minutes for x in incident):.1f} min",
              "- These are the cost of the incident, not strategy performance, and are excluded from section 3."]
    else:
        L.append("None reconciled.")
    paper = [r for r in rows if r.klass == CLASS_PAPER]
    L += ["", "## 5. Paper trades", "", f"{len(paper)} paper trades exist in the bundle. They carry no real fees or slippage and are not compared with live results here.", "",
          "## 6. Verdict", ""]
    v = verdict([x.r_net for x in normal if x.r_net is not None], status)
    L += [f"**{v['label']}**: {v['text']}", "",
          "## 7. Limits", "",
          "- One market period; results do not carry over to other regimes.",
          "- Bootstrap intervals assume independent trades; trades cluster in time, so real uncertainty is larger.",
          "- Fees paid in BNB are not converted; trades with them are flagged.",
          "- Slippage is measured against the signal price captured by the bot, not against a tick-level mid price.",
          "- The ledger covers trades the bot made on one account; it says nothing about other customers' accounts.", ""]
    return "\n".join(L)


def csv_rows(rows: list[Economics]) -> str:
    import csv
    import io

    cols = ["trade_ref", "klass", "pair", "side", "close_reason", "reconciled", "problems", "quantity", "entry_avg", "exit_avg", "gross_pnl", "fees_usdt",
            "funding_usdt", "net_pnl", "planned_risk", "r_net", "entry_slip_bps", "stop_slip_bps", "entry_fee_bps", "exit_fee_bps", "hold_minutes"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([("; ".join(r.problems) if c == "problems" else getattr(r, c)) for c in cols])
    return buf.getvalue()
