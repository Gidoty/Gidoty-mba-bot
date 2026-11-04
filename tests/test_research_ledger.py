"""Trade ledger, cost calibration and the ledger report (design 4). Synthetic Binance-shaped rows only; no network, no
credentials. These tests prove the arithmetic and the guards, not anything about real trading results."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mba_bot.research import cost_calibration as cc
from mba_bot.research import ledger as lg
from mba_bot.research import ledger_report as lr
from mba_bot.research.__main__ import main as research_main
from mba_bot.research.engine import CostModel

ROOT = Path(__file__).parent.parent
PREREG = ROOT / "docs" / "research" / "phase3_ledger_preregistration.md"
T0 = 1_760_000_000_000


def make(i, *, side="long", pair="BTC/USDT", hold_min=120, signal=100.0, stop=98.0, qty=1.0, entry_fill=None, exit_fill=None,
         close_reason="crossover_exit", fee_bps=5.0, funding=-0.01, maker=False, asset="USDT", paper=False, start=None):
    sym = pair.replace("/", "")
    sign = 1 if side == "long" else -1
    entry_fill = entry_fill if entry_fill is not None else signal + sign * 0.05
    exit_fill = exit_fill if exit_fill is not None else signal + sign * 2.0
    opened = (start if start is not None else T0 + i * 6 * 3_600_000)
    closed = opened + hold_min * 60_000
    e_side, x_side = ("BUY", "SELL") if side == "long" else ("SELL", "BUY")
    trade = lg.LedgerTrade(trade_ref=f"t{i}", pair=pair, side=side, is_paper=paper, status="closed", close_reason=close_reason,
                           opened_ms=opened, closed_ms=closed, signal_price=signal, entry_price=signal, exit_price=exit_fill,
                           stop_price=stop, quantity=qty, pnl_recorded=None)
    fills = []
    for k, part in enumerate((0.4, 0.6)):
        q = qty * part
        fills.append(lg.Fill(opened + 1000 + k, sym, e_side, entry_fill, q, q * entry_fill, q * entry_fill * fee_bps / 1e4, asset, maker, 0.0, f"e{i}"))
    realized = (exit_fill - entry_fill) * qty * sign
    fills.append(lg.Fill(closed - 500, sym, x_side, exit_fill, qty, qty * exit_fill, qty * exit_fill * fee_bps / 1e4, asset, maker, realized, f"x{i}"))
    income = [lg.Income(opened + 60_000, sym, "FUNDING_FEE", funding, "USDT")] if funding else []
    return trade, fills, income


def ledger_of(*parts):
    trades = [p[0] for p in parts]
    fills = [f for p in parts for f in p[1]]
    income = [i for p in parts for i in p[2]]
    return lg.build_ledger(trades, fills, income)


# ------------------------------------------------------------------ arithmetic


def test_long_trade_economics_are_exact():
    [e] = ledger_of(make(1))
    assert e.reconciled and e.klass == lg.CLASS_NORMAL and e.problems == []
    assert e.gross_pnl == pytest.approx(1.95)
    assert e.fees_usdt == pytest.approx(0.101025)
    assert e.funding_usdt == pytest.approx(-0.01)
    assert e.net_pnl == pytest.approx(1.838975)
    assert e.planned_risk == pytest.approx(2.0) and e.r_net == pytest.approx(0.9194875)
    assert e.entry_slip_bps == pytest.approx(5.0) and e.stop_slip_bps is None
    assert e.entry_fee_bps == pytest.approx(5.0) and e.exit_fee_bps == pytest.approx(5.0)
    assert e.entry_taker and e.exit_taker and e.hold_minutes == pytest.approx(120)


def test_short_trade_and_adverse_sign_conventions():
    [e] = ledger_of(make(1, side="short", signal=100.0, stop=102.0, entry_fill=99.9, exit_fill=98.0))
    assert e.reconciled
    assert e.gross_pnl == pytest.approx((98.0 - 99.9) * 1.0 * -1)  # +1.9
    assert e.entry_slip_bps == pytest.approx(10.0)  # selling lower than the signal is adverse


def test_stop_exit_slippage_is_adverse_positive():
    [e] = ledger_of(make(1, close_reason="stop_loss", stop=98.0, exit_fill=97.9))
    assert e.stop_slip_bps == pytest.approx((98.0 - 97.9) / 98.0 * 1e4)
    [fav] = ledger_of(make(2, close_reason="stop_loss", stop=98.0, exit_fill=98.1))
    assert fav.stop_slip_bps < 0  # filled better than the stop


def test_funding_counts_only_inside_the_holding_window():
    t, f, _ = make(1, funding=0)
    sym = "BTCUSDT"
    inc = [lg.Income(t.opened_ms - 1, sym, "FUNDING_FEE", -9.0, "USDT"), lg.Income(t.opened_ms + 1, sym, "FUNDING_FEE", -0.2, "USDT"),
           lg.Income(t.closed_ms, sym, "FUNDING_FEE", 0.05, "USDT"), lg.Income(t.closed_ms + 1, sym, "FUNDING_FEE", -9.0, "USDT"),
           lg.Income(t.opened_ms + 5, "ETHUSDT", "FUNDING_FEE", -9.0, "USDT")]
    [e] = lg.build_ledger([t], f, inc)
    assert e.funding_usdt == pytest.approx(-0.15)


# ------------------------------------------------------------------ reconciliation guards


def test_a_manual_trade_on_the_same_account_makes_the_trade_unreconciled_not_wrong():
    t, f, i = make(1)
    extra = lg.Fill(t.opened_ms + 5000, "BTCUSDT", "BUY", 100.0, 0.5, 50.0, 0.025, "USDT", False, 0.0, "manual")
    [e] = lg.build_ledger([t], f + [extra], i)
    assert not e.reconciled and any("net to flat" in p for p in e.problems)
    assert lg.usable([e]) == []


def test_missing_fills_quantity_mismatch_and_bnb_commission_are_unreconciled():
    t, f, i = make(1)
    [none] = lg.build_ledger([t], [], i)
    assert not none.reconciled and "no matching entry and exit fills" in none.problems
    t2, f2, i2 = make(2, qty=1.0)
    t2 = lg.LedgerTrade(**{**t2.__dict__, "quantity": 5.0})
    [mis] = lg.build_ledger([t2], f2, i2)
    assert not mis.reconciled and any("recorded quantity" in p for p in mis.problems)
    [bnb] = ledger_of(make(3, asset="BNB"))
    assert not bnb.reconciled and bnb.fees_other_assets and any("another asset" in p for p in bnb.problems)


def test_computed_gross_must_agree_with_the_exchange_realized_pnl():
    t, f, i = make(1)
    bad = [lg.Fill(**{**x.__dict__, "realized_pnl": x.realized_pnl + 5.0}) if x.side == "SELL" else x for x in f]
    [e] = lg.build_ledger([t], bad, i)
    assert not e.reconciled and any("realizedPnl" in p for p in e.problems)


def test_classification_and_counts():
    parts = [make(1), make(2, close_reason="protection_failed_emergency_exit", hold_min=1), make(3, close_reason="protection_transient_emergency_exit", hold_min=1),
             make(4, close_reason="protection_unclassified_emergency_exit", hold_min=1), make(5, paper=True)]
    rows = ledger_of(*parts)
    assert [r.klass for r in rows] == ["normal", "incident", "incident", "incident", "paper"]
    c = lg.counts(rows)
    assert c["normal"]["reconciled"] == 1 and c["incident"]["reconciled"] == 3 and c["paper"]["all"] == 1


def test_emergency_reasons_match_the_service_when_the_service_has_them():
    pg = pytest.importorskip("mba_bot.protection_guard", reason="this branch predates the protection guard")
    assert lg.EMERGENCY_REASONS == {pg.EMERGENCY_REASON_STRUCTURAL, pg.EMERGENCY_REASON_TRANSIENT, pg.EMERGENCY_REASON_OTHER}


# ------------------------------------------------------------------ the bundle


def bundle_text(parts, **extra):
    raw = {"schema": lg.SCHEMA, "exported_at_ms": 1, "window": {}, "salt_id": "x", **extra,
           "trades": [{"trade_ref": t.trade_ref, "pair": t.pair, "side": t.side, "is_paper": t.is_paper, "status": t.status, "close_reason": t.close_reason,
                       "opened_ms": t.opened_ms, "closed_ms": t.closed_ms, "signal_price": t.signal_price, "entry_price": t.entry_price,
                       "exit_price": t.exit_price, "stop_price": t.stop_price, "quantity": t.quantity, "pnl": t.pnl_recorded} for t, _, _ in parts],
           "fills": [{"time_ms": f.time_ms, "symbol": f.symbol, "side": f.side, "price": f.price, "qty": f.qty, "quote_qty": f.quote_qty,
                      "commission": f.commission, "commission_asset": f.commission_asset, "maker": f.maker, "realized_pnl": f.realized_pnl,
                      "order_ref": f.order_ref} for _, fl, _ in parts for f in fl],
           "income": [{"time_ms": i.time_ms, "symbol": i.symbol, "kind": i.kind, "amount": i.amount, "asset": i.asset} for _, _, inc in parts for i in inc]}
    return json.dumps(raw)


def test_bundle_round_trip_and_rejections():
    parts = [make(1), make(2)]
    trades, fills, income, meta = lg.load_bundle(bundle_text(parts))
    assert len(trades) == 2 and len(fills) == 6 and len(income) == 2
    for bad in ("not json", json.dumps({"schema": "other"}), json.dumps([])):
        with pytest.raises(lg.BundleError):
            lg.load_bundle(bad)
    raw = json.loads(bundle_text(parts))
    for mutate in (lambda r: r.update(notes="x"), lambda r: r["trades"][0].update(account_id="x"), lambda r: r["trades"][0].update(email="a@b.c"),
                   lambda r: r["fills"][0].update(api_key="k"), lambda r: r["income"][0].update(extra=1), lambda r: r["window"].update(token="t") if r.get("window") else r.update(window={"token": "t"})):
        bad = json.loads(json.dumps(raw))
        mutate(bad)
        with pytest.raises(lg.BundleError, match="not in the bundle format"):
            lg.load_bundle(json.dumps(bad))
    with pytest.raises(lg.BundleError):
        lg.load_bundle(json.dumps({"schema": lg.SCHEMA, "trades": [{"pair": "BTC/USDT"}]}))


# ------------------------------------------------------------------ calibration


def many(n, **kw):
    return ledger_of(*[make(i, **kw) for i in range(n)])


def test_measured_costs_replace_assumptions_only_with_enough_observations():
    small = cc.measure(many(10, fee_bps=4.0))
    model, prov = cc.build_cost_model(small)
    assert model.taker_fee_bps == 5.0 and prov["taker_fee_bps"] == "assumed"
    big = cc.measure(many(40, fee_bps=4.0, entry_fill=100.03))
    model, prov = cc.build_cost_model(big)
    assert model.taker_fee_bps == pytest.approx(4.0) and prov["taker_fee_bps"].startswith("measured (n=")
    assert model.market_slip_bps == pytest.approx(3.0) and prov["market_slip_bps"].startswith("measured")  # 3 bp entry slippage
    assert prov["stop_slip_bps"] == "assumed"  # no stop exits in this sample
    assert isinstance(model, CostModel)


def test_pair_specific_slippage_needs_enough_observations_for_that_pair():
    rows = many(40, pair="XRP/USDT", close_reason="stop_loss", signal=2.0, stop=1.9, entry_fill=2.0004, exit_fill=1.8996)
    model, prov = cc.build_cost_model(cc.measure(rows))
    assert "XRPUSDT" in model.per_symbol_slip_bps and "XRPUSDT" in prov["per_symbol_slip_bps"]
    assert "ETHUSDT" in model.per_symbol_slip_bps and "ETHUSDT" not in prov["per_symbol_slip_bps"]  # default kept, not claimed measured


def test_time_ordered_validation_never_claims_what_it_cannot_support():
    assert cc.validate_in_time(many(20))["status"] == "not validated"
    ok = cc.validate_in_time(many(60, entry_fill=100.02))
    assert ok["status"] == "checked" and ok["drift"] == []
    drifting = ledger_of(*[make(i, entry_fill=100.02) for i in range(36)], *[make(36 + i, entry_fill=100.40) for i in range(24)])
    d = cc.validate_in_time(drifting)
    assert d["status"] == "checked" and d["drift"] == ["entry_slip_bps"]


# ------------------------------------------------------------------ report and verdict


def write_prereg(tmp_path, line):
    p = tmp_path / "prereg.md"
    p.write_text(f"# x\n\n{line}\n")
    return p


@pytest.mark.parametrize("line,status", [("Owner agreement: AGREED 2026-11-05", "AGREED"), ("Owner agreement: PENDING", "PENDING"),
                                         ("nothing here", "UNCLEAR"), ("Owner agreement: AGREED\nOwner agreement: PENDING", "UNCLEAR")])
def test_prereg_status_parsing(tmp_path, line, status):
    assert lr.prereg_status(write_prereg(tmp_path, line))[0] == status
    assert lr.prereg_status(tmp_path / "missing.md")[0] == "MISSING"


def test_the_shipped_registration_is_pending_and_its_constants_match_the_code():
    assert lr.prereg_status(PREREG)[0] == "PENDING"
    text = PREREG.read_text()
    block = dict(re.findall(r"^([A-Z_]+) = ([0-9.]+)$", text, re.M))
    assert float(block["N_MIN_VERDICT"]) == lr.N_MIN_VERDICT
    assert float(block["MIN_OBS_COST"]) == cc.MIN_OBS_COST
    assert float(block["MIN_TRADES_HOLDOUT"]) == cc.MIN_TRADES_HOLDOUT
    assert float(block["FIT_FRACTION"]) == cc.FIT_FRACTION
    for name in ("QTY_TOLERANCE", "FLAT_TOLERANCE"):
        assert getattr(lg, name) in (0.02, 0.005)  # the numbers quoted in section 2


def test_verdict_labels():
    assert lr.verdict([1.0] * 10, "AGREED")["label"] == "INSUFFICIENT SAMPLE"
    assert lr.verdict([1.0, 0.5, 0.8] * 12, "PENDING")["label"] == "WITHHELD"
    assert lr.verdict([1.0, 0.5, 0.8] * 12, "AGREED")["label"] == "POSITIVE"
    assert lr.verdict([-1.0, -0.5, -0.8] * 12, "AGREED")["label"] == "NEGATIVE"
    assert lr.verdict([1.0, -1.0, 0.1, -0.1] * 10, "AGREED")["label"] == "NO EDGE DETECTED"


def test_report_orders_sections_separates_incidents_and_withholds_without_agreement(tmp_path):
    rows = ledger_of(*[make(i, hold_min=120) for i in range(40)], *[make(100 + i, close_reason="protection_failed_emergency_exit", hold_min=1,
                                                                         exit_fill=99.5, funding=0) for i in range(5)], make(200, paper=True))
    text = lr.render(rows, {}, prereg_path=str(write_prereg(tmp_path, "Owner agreement: PENDING")))
    order = [text.index(h) for h in ("## 1. What the sample is", "## 2. Costs", "## 3. Results", "## 4. Incident trades", "## 5. Paper", "## 6. Verdict", "## 7. Limits")]
    assert order == sorted(order)
    assert "**WITHHELD**" in text and "POSITIVE" not in text.split("## 6. Verdict")[1].split("## 7.")[0]
    assert "5 reconciled" in text and "excluded from section 3" in text
    section3 = text.split("## 3. Results")[1].split("## 4.")[0]
    assert "trades 40" in section3  # incident and paper rows are not in the strategy result
    t2, f2, i2 = make(300)
    stray = lg.Fill(t2.opened_ms + 5000, "BTCUSDT", "BUY", 100.0, 0.5, 50.0, 0.025, "USDT", False, 0.0, "manual")
    more = lg.build_ledger([t2], f2 + [stray], i2)
    assert not more[0].reconciled
    again = lr.render(rows + more, {}, prereg_path=str(write_prereg(tmp_path, "Owner agreement: PENDING")))
    assert "trades 40" in again.split("## 3. Results")[1].split("## 4.")[0]  # an unreconciled trade changes no number
    agreed = lr.render(rows, {}, prereg_path=str(write_prereg(tmp_path, "Owner agreement: AGREED 2026-11-05")))
    assert "**POSITIVE**" in agreed
    thin = lr.render(rows[:10], {}, prereg_path=str(write_prereg(tmp_path, "Owner agreement: AGREED 2026-11-05")))
    assert "**INSUFFICIENT SAMPLE**" in thin


def test_the_report_contains_no_identifying_material(tmp_path):
    rows = ledger_of(*[make(i) for i in range(35)])
    text = lr.render(rows, {"salt_id": "abc"}, prereg_path=str(PREREG)) + lr.csv_rows(rows)
    for needle in ("secret", "@", "bot_code", "password", "api_key", "account_id", "signature"):
        assert needle not in text.lower(), needle
    body = "\n".join(l for l in text.splitlines() if not l.startswith("Registration:"))  # that line carries the registration's own sha256
    assert not re.search(r"[0-9a-f]{20,}", body)  # no long hex tokens (hashes, keys, ids)


def test_cli_writes_the_report_and_refuses_a_bundle_with_forbidden_fields(tmp_path, capsys):
    parts = [make(i) for i in range(35)]
    good = tmp_path / "b.json"
    good.write_text(bundle_text(parts))
    assert research_main(["ledger", "--bundle", str(good), "--prereg", str(PREREG), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "ledger_report.md").read_text().count("WITHHELD") >= 1
    assert (tmp_path / "out" / "ledger_trades.csv").read_text().startswith("trade_ref,")
    bad = tmp_path / "bad.json"
    bad.write_text(bundle_text(parts, account_id="x"))
    assert research_main(["ledger", "--bundle", str(bad), "--out", str(tmp_path / "out2")]) == 2
    assert not (tmp_path / "out2").exists()
