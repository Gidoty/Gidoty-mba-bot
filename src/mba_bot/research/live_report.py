"""Read-only analysis of the bot's own recorded trades and decisions.

Opens the SQLite database with `mode=ro` (the file cannot be modified through this connection),
reads `trades`, `trade_telemetry` and `decision_log`, and expresses live and paper results in the
same terms as the backtest (R-multiples, slippage, modelled costs). It reads no credential table,
no customer email and no secret: accounts appear only as an anonymous short id.

Costs here are MODELLED (fee rate x notional on both legs), because exact exchange fees are not
recorded by the trading path; they are labelled as estimates. Slippage is measured: the recorded fill
against the signal price captured at decision time, and a stop exit against its stop price.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("select name from sqlite_master where type='table'")}


def _ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def live_report(db_path: str, *, fee_bps: float = 5.0) -> dict:
    con = _connect(db_path)
    try:
        tables = _tables(con)
        out: dict = {"database": Path(db_path).name, "generated_at": datetime.now(timezone.utc).isoformat(), "fee_model_bps_per_side": fee_bps}
        has_tel = "trade_telemetry" in tables
        rows = con.execute(
            "select t.id, substr(t.account_id,1,8) as acct, t.side, t.is_paper, t.entry_price, t.quantity, t.stop_loss_price, t.exit_price, t.pnl, "
            "t.close_reason, t.status, t.pair, t.opened_at, t.closed_at"
            + (", tt.signal_price, tt.strategy_id, tt.pair_selection_mode, tt.planned_risk_amount, tt.atr_pct, tt.features_json, tt.entry_fee_quote, tt.exit_fee_quote, tt.funding_quote "
               if has_tel else ", null as signal_price, null as strategy_id, null as pair_selection_mode, null as planned_risk_amount, null as atr_pct, null as features_json, null as entry_fee_quote, null as exit_fee_quote, null as funding_quote ")
            + "from trades t " + ("left join trade_telemetry tt on tt.trade_id = t.id " if has_tel else "")
            + "where t.status = 'closed' and t.pnl is not null"
        ).fetchall()
        trades = []
        for r in rows:
            risk = r["quantity"] * abs(r["entry_price"] - r["stop_loss_price"]) if r["entry_price"] and r["stop_loss_price"] else None
            notional = r["quantity"] * r["entry_price"]
            est_fee = 2 * fee_bps / 1e4 * notional
            exact = r["entry_fee_quote"] is not None and r["exit_fee_quote"] is not None
            fee = (r["entry_fee_quote"] + r["exit_fee_quote"]) if exact else est_fee
            funding = r["funding_quote"] or 0.0
            net = r["pnl"] - fee - funding
            opened, closed = _ts(r["opened_at"]), _ts(r["closed_at"])
            sign = 1 if r["side"] == "long" else -1
            entry_slip_bps = ((r["entry_price"] - r["signal_price"]) / r["signal_price"] * sign * 1e4) if r["signal_price"] else None
            stop_slip_bps = ((r["stop_loss_price"] - r["exit_price"]) / r["stop_loss_price"] * sign * 1e4) if r["close_reason"] in ("stop_loss", "exchange_stop_triggered") and r["stop_loss_price"] else None
            trades.append({
                "acct": r["acct"], "mode": "paper" if r["is_paper"] else "live", "pair": r["pair"], "side": r["side"], "reason": r["close_reason"],
                "r_gross": r["pnl"] / risk if risk else None, "r_net_est": net / risk if risk else None, "pnl_gross": r["pnl"], "fee": fee, "fee_exact": exact,
                "net_est": net, "hold_h": (closed - opened).total_seconds() / 3600 if opened and closed else None,
                "entry_slip_bps": entry_slip_bps, "stop_slip_bps": stop_slip_bps, "selection": r["pair_selection_mode"], "strategy": r["strategy_id"],
                "has_telemetry": r["signal_price"] is not None,
            })
        out["closed_trades"] = len(trades)
        out["groups"] = {m: _stats([t for t in trades if t["mode"] == m]) for m in ("live", "paper")}
        out["by_pair"] = {p: _stats([t for t in trades if t["pair"] == p]) for p in sorted({t["pair"] for t in trades if t["pair"]})}
        out["by_close_reason"] = {p: _stats([t for t in trades if t["reason"] == p]) for p in sorted({t["reason"] for t in trades if t["reason"]})}
        slips = [t["entry_slip_bps"] for t in trades if t["entry_slip_bps"] is not None]
        stops = [t["stop_slip_bps"] for t in trades if t["stop_slip_bps"] is not None]
        out["measured_slippage_bps"] = {
            "entry": _dist(slips), "stop_exit": _dist(stops),
            "note": "positive = worse for the trader; entry compares the recorded fill with the signal price captured at decision time",
        }
        out["telemetry_coverage"] = {"closed_trades_with_telemetry": sum(1 for t in trades if t["has_telemetry"]), "of": len(trades)}
        out["decisions"] = _decisions(con, tables)
        return out
    finally:
        con.close()


def _dist(values: list[float]) -> dict | None:
    if not values:
        return None
    s = sorted(values)
    return {"n": len(s), "mean": statistics.fmean(s), "median": statistics.median(s), "p90": s[int(0.9 * (len(s) - 1))], "max": s[-1]}


def _stats(ts: list[dict]) -> dict:
    n = len(ts)
    if not n:
        return {"n": 0}
    r = [t["r_net_est"] for t in ts if t["r_net_est"] is not None]
    wins = [t for t in ts if t["net_est"] > 0]
    gains = sum(t["net_est"] for t in ts if t["net_est"] > 0)
    losses = sum(t["net_est"] for t in ts if t["net_est"] <= 0)
    return {
        "n": n, "win_rate": len(wins) / n, "expectancy_r_net_est": statistics.fmean(r) if r else None,
        "expectancy_r_gross": statistics.fmean([t["r_gross"] for t in ts if t["r_gross"] is not None]) if any(t["r_gross"] is not None for t in ts) else None,
        "profit_factor": (gains / abs(losses)) if losses < 0 else None, "net_est": sum(t["net_est"] for t in ts),
        "fees": sum(t["fee"] for t in ts), "fees_exact_for": sum(1 for t in ts if t["fee_exact"]),
        "avg_hold_h": statistics.fmean([t["hold_h"] for t in ts if t["hold_h"] is not None]) if any(t["hold_h"] is not None for t in ts) else None,
    }


def _decisions(con: sqlite3.Connection, tables: set[str]) -> dict:
    if "decision_log" not in tables:
        return {"available": False}
    rows = con.execute("select stage, outcome, reason_code, mode, count(*) c from decision_log group by 1,2,3,4 order by c desc").fetchall()
    first = con.execute("select min(ts), max(ts), count(*) from decision_log").fetchone()
    return {
        "available": True, "rows": first[2], "first": first[0], "last": first[1],
        "by_reason": [{"stage": r["stage"], "outcome": r["outcome"], "reason": r["reason_code"], "mode": r["mode"], "count": r["c"]} for r in rows],
    }


def render(report: dict) -> str:
    L = [f"# Live and paper results ({report['database']})", "", f"Closed trades: {report['closed_trades']} | costs are MODELLED at {report['fee_model_bps_per_side']} bp per side unless exact fees were recorded", ""]
    for name, g in report["groups"].items():
        if g["n"]:
            L.append(f"- **{name}**: {g['n']} trades, win rate {g['win_rate']:.1%}, E[R] net (est.) {g['expectancy_r_net_est']:+.3f}, gross {g['expectancy_r_gross']:+.3f}, PF {g['profit_factor'] if g['profit_factor'] is None else round(g['profit_factor'], 2)}, net {g['net_est']:+.2f}")
    for label, key in (("By pair", "by_pair"), ("By close reason", "by_close_reason")):
        L += ["", f"## {label}", ""]
        for k, g in report[key].items():
            L.append(f"- {k}: {g['n']} trades, win {g['win_rate']:.0%}, E[R] net (est.) {g['expectancy_r_net_est'] if g['expectancy_r_net_est'] is None else round(g['expectancy_r_net_est'], 3)}")
    sl = report["measured_slippage_bps"]
    L += ["", "## Measured slippage (bp, positive = worse)", "", f"- entry: {sl['entry']}", f"- stop exits: {sl['stop_exit']}", "", f"Telemetry coverage: {report['telemetry_coverage']}"]
    d = report["decisions"]
    if d.get("available"):
        L += ["", f"## Decision log ({d['rows']} rows, {d['first']} to {d['last']})", ""]
        for r in d["by_reason"][:25]:
            L.append(f"- {r['stage']}/{r['outcome']}/{r['reason']} ({r['mode']}): {r['count']}")
    return "\n".join(L) + "\n"
