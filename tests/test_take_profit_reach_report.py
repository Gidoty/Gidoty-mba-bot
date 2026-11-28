"""The take-profit reach report (ops/reports/take_profit_reach_report.py), run against a temporary database.

PROVES: it opens the database read-only (the file is byte-identical afterwards, a missing file is not created, and a write attempt through its connection fails); it lists only closed live and practice trades with a best
price and an initial stop and counts the rest as skipped (demo trades are left out); R, peak R and the levels reached are right for longs and shorts (a level is reached at exactly the level); the hypothetical result
is the level minus the estimated cost for a trade that reached it and the trade's own result otherwise; the actual net follows the app's rule (fees and funding known, Spot has no funding, practice has none); the owner's
trades are listed by short code and other customers' only inside aggregates; no email, account id or customer data is printed.
DOES NOT PROVE: anything about the real database or about Binance."""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from helpers_live import make_account, make_settings

from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("take_profit_reach_report", ROOT / "ops" / "reports" / "take_profit_reach_report.py")
rep = importlib.util.module_from_spec(spec)
sys.modules["take_profit_reach_report"] = rep
spec.loader.exec_module(rep)

OWNER = "owner-person@example.com"
OTHER = "someone-else@example.com"


@pytest.fixture
def dbfile(tmp_path):
    path = tmp_path / "t.db"
    init_db(f"sqlite:///{path}")
    return path


def add_trade(account_id, **kw):
    base = dict(account_id=account_id, side="long", is_paper=False, entry_price=100.0, quantity=2.0, stop_loss_price=98.0, initial_stop_price=98.0, peak_price=104.0,
                exit_price=101.0, pnl=2.0, status="closed", pair="BTC/USDT", protective_kind="stop_market_close", entry_fee_quote=0.1, exit_fee_quote=0.1, funding_quote=0.0)
    base.update(kw)
    with session_scope() as s:
        t = Trade(**base)
        s.add(t)
        s.flush()
        return t.id


def accounts():
    settings = make_settings()
    with session_scope() as s:
        a = make_account(s, encryption_key=settings.encryption_key, email=OWNER, market_type="futures")
        b = make_account(s, encryption_key=settings.encryption_key, email=OTHER, market_type="futures")
    return a, b


def run(dbfile, capsys, *args):
    code = rep.main(["--db", str(dbfile), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_levels_cost_and_hypothetical_results_for_a_long(dbfile, capsys):
    a, _ = accounts()
    tid = add_trade(a)  # entry 100, initial stop 98 (R = 2), peak 104 (= 2R), gross +2.0 on 2 units
    code, out, _ = run(dbfile, capsys, "--owner-email", OWNER)
    assert code == 0
    row = [line for line in out.splitlines() if line.strip().startswith(tid[:8])][0]
    assert " 2.00 " in row and "1,1.5,2" in row.replace(" ", "")  # peak R 2.00, levels 1, 1.5 and 2 reached
    cost = 100.0 * 2.0 * (2 * 5.0 + 11.0) / 10_000.0  # 0.42
    r_quote = 2.0 * 2.0  # R (2.0 of price) x quantity (2.0)
    assert f"TP2 {2.0 * r_quote - cost:+.4f}" in row  # reached at exactly 2R: closes at the level minus the estimated cost
    assert f"TP1 {1.0 * r_quote - cost:+.4f}" in row
    assert "TP3 -" in row and "TP5 -" in row  # not reached
    assert "+1.8000 net" in row  # actual: 2.0 - 0.1 - 0.1 + 0 funding = 1.8 net


def test_a_short_mirrors_and_a_level_is_reached_at_exactly_the_level(dbfile, capsys):
    a, _ = accounts()
    tid = add_trade(a, side="short", entry_price=100.0, initial_stop_price=102.0, stop_loss_price=102.0, peak_price=97.0, pnl=-1.0, exit_price=100.5)  # R = 2, peak 3 below = 1.5R
    _, out, _ = run(dbfile, capsys, "--owner-email", OWNER)
    row = [line for line in out.splitlines() if line.strip().startswith(tid[:8])][0]
    assert "1.50" in row and "1,1.5" in row.replace(" ", "") and "TP2 -" in row
    assert "TP1.5 +" in row


def test_trades_without_peak_or_initial_stop_or_open_or_demo_are_not_used(dbfile, capsys):
    a, _ = accounts()
    add_trade(a)
    add_trade(a, peak_price=None)
    add_trade(a, initial_stop_price=None)
    add_trade(a, status="open")
    add_trade(a, status="void")
    add_trade(a, is_demo=True)
    add_trade(a, initial_stop_price=100.0)  # R = 0
    code, out, _ = run(dbfile, capsys)
    assert code == 0
    assert "found: 4. Used: 1. Skipped: 3 (" in out and "no_peak_or_initial_stop: 2" in out and "zero_r: 1" in out


def test_the_actual_net_follows_the_apps_rule(dbfile, capsys):
    a, _ = accounts()
    known = add_trade(a, funding_quote=-0.05)  # fees 0.2, funding -0.05: net 2.0 - 0.2 - 0.05 = 1.75
    unknown_funding = add_trade(a, funding_quote=None)  # a Futures trade whose funding was not read: no net
    spot = add_trade(a, protective_kind="stop_loss", funding_quote=None)  # Spot: no funding exists: net known
    no_fee = add_trade(a, entry_fee_quote=None)
    paper = add_trade(a, is_paper=True)
    _, out, _ = run(dbfile, capsys, "--owner-email", OWNER)
    rows = {t: [line for line in out.splitlines() if line.strip().startswith(t[:8])] for t in (known, unknown_funding, spot, no_fee, paper)}
    assert "+1.7500 net" in rows[known][0]
    assert "gross (net unknown)" in rows[unknown_funding][0]
    assert "+1.8000 net" in rows[spot][0]
    assert "gross (net unknown)" in rows[no_fee][0]
    assert rows[paper] == []  # a practice trade is never listed individually, only in the aggregates
    assert "practice" in out


def test_only_the_owners_live_trades_are_listed_and_nothing_identifying_is_printed(dbfile, capsys):
    a, b = accounts()
    mine = add_trade(a)
    theirs = add_trade(b)
    with session_scope() as s:
        ids = {x.account_id for x in s.query(Trade).all()}
        from mba_bot.models import Customer, TradingAccount

        emails = [c.email for c in s.query(Customer).all()]
        codes = [x.bot_code for x in s.query(TradingAccount).all()]
    _, out, err = run(dbfile, capsys, "--owner-email", OWNER)
    assert mine[:8] in out and theirs[:8] not in out
    for needle in [OWNER, OTHER, "example.com", *ids, *emails, *codes, "owner-person", "someone-else", "password", "synthetic-"]:
        assert needle not in out and needle not in err, needle
    _, out2, _ = run(dbfile, capsys)  # without --owner-email no trade is listed at all
    assert mine[:8] not in out2 and "--owner-email was not given" in out2


def test_the_owner_email_match_ignores_case(dbfile, capsys):
    a, _ = accounts()
    tid = add_trade(a)
    _, out, _ = run(dbfile, capsys, "--owner-email", OWNER.upper())
    assert tid[:8] in out


def test_aggregates_are_by_kind_strategy_and_pair(dbfile, capsys):
    a, b = accounts()
    add_trade(a)
    add_trade(b, pair="ETH/USDT", strategy_id="donchian_breakout_v1")
    add_trade(b, is_paper=True, pair="XRP/USDT")
    _, out, _ = run(dbfile, capsys)
    assert "== LIVE: all strategies and pairs" in out and "== PRACTICE: all strategies and pairs" in out
    assert "-- live / ema_cross_atr_v1 / BTC/USDT" in out and "-- live / donchian_breakout_v1 / ETH/USDT" in out and "-- practice / ema_cross_atr_v1 / XRP/USDT" in out


def test_the_share_of_trades_reaching_each_level_and_the_change_are_right(dbfile, capsys):
    a, _ = accounts()
    add_trade(a, peak_price=104.0, pnl=2.0)  # 2R, reached 1, 1.5, 2
    add_trade(a, peak_price=100.5, pnl=-2.0)  # 0.25R, none
    _, out, _ = run(dbfile, capsys)
    block = out.split("== LIVE: all strategies and pairs")[1].split("\n\n")[0]
    line = {lv: [x for x in block.splitlines() if x.strip().startswith(f"TP {lv}")][0] for lv in ("1", "1.5", "2", "3")}
    assert "1 ( 50%)" in line["1"] and "1 ( 50%)" in line["2"] and "0 (  0%)" in line["3"]
    r_quote = 4.0
    cost = 100.0 * 2.0 * 21.0 / 10_000.0
    basis = ((2.0 - cost) / r_quote + (-2.0 - cost) / r_quote) / 2
    hyp2 = ((2.0 - cost / r_quote) + (-2.0 - cost) / r_quote) / 2
    assert f"{basis:+.3f} R" in block and f"{hyp2:+.3f} R" in line["2"] and f"{hyp2 - basis:+.3f} R" in line["2"]


def test_the_cost_assumption_is_stated_and_can_be_changed(dbfile, capsys):
    a, _ = accounts()
    add_trade(a)
    _, out, _ = run(dbfile, capsys)
    assert "21 bps of the entry price" in out and "This is an assumption." in out
    _, out2, _ = run(dbfile, capsys, "--fee-bps", "4.5", "--slip-rt-bps", "11")
    assert "20 bps of the entry price" in out2


def test_the_database_is_opened_read_only(dbfile, capsys, tmp_path):
    a, _ = accounts()
    add_trade(a)
    before = digest(dbfile)
    code, _, _ = run(dbfile, capsys, "--owner-email", OWNER)
    assert code == 0 and digest(dbfile) == before
    con = rep.open_readonly(str(dbfile))
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM trades")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("CREATE TABLE x (a int)")
    con.close()
    assert digest(dbfile) == before


def test_a_missing_database_is_not_created_and_the_message_is_plain(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    code = rep.main(["--db", str(missing)])
    out = capsys.readouterr()
    assert code == 2 and not missing.exists() and "Could not read the database read-only" in out.err and "Traceback" not in out.err


def test_the_script_has_no_write_path_and_imports_nothing_from_the_bot():
    src = (ROOT / "ops" / "reports" / "take_profit_reach_report.py").read_text()
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert "mode=ro" in src and "query_only" in src
    for forbidden in ("mba_bot", "INSERT", "UPDATE", "DELETE", "commit(", "open(", "write(", "os.environ", "getenv", ".env"):
        assert forbidden not in code.split('"""', 2)[2], forbidden


def _row(**kw):
    base = dict(id="abcdef1234567890", side="long", is_paper=0, entry_price=100.0, exit_price=101.0, quantity=2.0, pnl=2.0, initial_stop_price=98.0, peak_price=104.0, entry_fee_quote=0.1,
                exit_fee_quote=0.1, funding_quote=0.0, protective_kind="stop_market_close", strategy_id=None, pair="BTC/USDT", is_owner=0)
    base.update(kw)
    return base


def test_every_level_from_tp1_to_tp5_is_reported(dbfile, capsys):
    a, _ = accounts()
    add_trade(a, peak_price=108.5)  # 4.25R: reached 1, 1.5, 2, 3, 4 but not 5
    _, out, _ = run(dbfile, capsys, "--owner-email", OWNER)
    assert rep.LEVELS == (1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
    for lv in ("1", "1.5", "2", "3", "4", "5"):
        assert any(line.strip().startswith(f"TP {lv} ") for line in out.splitlines()), lv
    t, _ = rep.analyse(_row(peak_price=108.5), fee_bps=5.0, slip_rt_bps=11.0)
    assert [lv for lv in rep.LEVELS if t["reached"][lv]] == [1.0, 1.5, 2.0, 3.0, 4.0]


def test_a_best_price_on_the_losing_side_is_zero_r_and_reaches_nothing():
    t, _ = rep.analyse(_row(peak_price=99.0), fee_bps=5.0, slip_rt_bps=11.0)
    assert t["peak_r"] == 0.0 and not any(t["reached"].values())


def test_a_practice_trade_never_has_a_net_and_the_code_is_eight_characters():
    t, _ = rep.analyse(_row(is_paper=1), fee_bps=5.0, slip_rt_bps=11.0)
    assert t["net"] is None and t["kind"] == "practice" and len(t["code"]) == 8 and t["code"] == "abcdef12"
    live, _ = rep.analyse(_row(), fee_bps=5.0, slip_rt_bps=11.0)
    assert abs(live["net"] - 1.8) < 1e-9


def test_the_listing_prints_the_bare_eight_character_code(dbfile, capsys):
    a, _ = accounts()
    tid = add_trade(a)
    _, out, _ = run(dbfile, capsys, "--owner-email", OWNER)
    row = [line for line in out.splitlines() if line.strip().startswith(tid[:8])][0]
    assert row.split()[0] == tid[:8] and len(row.split()[0]) == 8


def test_the_connection_is_read_only_by_itself_and_by_the_pragma(dbfile):
    con = rep.open_readonly(str(dbfile))
    assert con.execute("PRAGMA query_only").fetchone()[0] == 1
    con.execute("PRAGMA query_only = OFF")  # even with the pragma lifted, the file was opened read-only
    with pytest.raises(sqlite3.OperationalError):
        con.execute("CREATE TABLE y (a int)")
    con.close()
