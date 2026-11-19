"""B6a: FUTURES_LEVERAGE_MODE = strict | size_cap and the customer's max_leverage (docs/safety/customer_leverage.md, docs/customer/leverage_help.md).

PROVES (fake exchange, scratch SQLite): empty reads as 3; strict (the default) ignores the customer's number and behaves as on main; size_cap sizes to min(Binance, customer) for
Binance below, equal and above and never refuses for a high number; isolated margin is required (cross and unreadable are refused with the fixed message); the liquidation check
still judges the REAL Binance leverage; size_cap without PROTECTION_PRECHECK_MODE=enforce behaves as strict and says so once an hour; an open trade and Spot are unaffected;
the migration is additive, idempotent and reversible; the help file and the code agree on every message.
DOES NOT PROVE: anything about Binance. The fake returns the shape ccxt 4.5.88's parse_leverage produces; a live reply was not read."""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import all_trades, futures_client, install, make_settings, new_account, open_protected, run_cycle, spot_client
from sqlalchemy import create_engine, inspect

from mba_bot import leverage as lv
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount

ROOT = Path(__file__).resolve().parent.parent
CAPITAL = 1000.0


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def entry(monkeypatch, *, binance=20, margin="isolated", cap=None, mode="size_cap", global_=5.0, precheck="enforce", mult=0.02, market="futures", **over):
    """One live Futures cycle with a TIGHT stop (so the size cap, not the risk percent, decides the size). Returns (trades, customer-visible refusals, settings, client)."""
    st = make_settings(max_notional_multiplier=100.0, futures_leverage_mode=mode, futures_max_leverage=global_, protection_precheck_mode=precheck, atr_stop_multiplier=mult,
                       trade_telemetry=True, **over)
    client = futures_client(leverage_by_symbol={"BTC/USDT": binance}, margin_mode=margin)
    install(monkeypatch, client)
    aid = new_account(st, market_type=market, allocated_capital=CAPITAL)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.risk_pct = 2.0
        a.max_leverage = cap
    notes = run_cycle(aid, st)
    trades = all_trades(aid)
    return [t.quantity * t.entry_price for t in trades], [x for x in notes.texts if "did not open a trade" in x], st, client


def reasons():
    with session_scope() as s:
        return [d.reason_code for d in s.query(DecisionLog).all()]


# ------------------------------------------------------------------ pure rules
def test_empty_and_bad_values_read_as_three():
    for bad in (None, 0, 6, -1, 2.5, "3", True, False, float("nan")):
        assert lv.stored_or_default(bad) == 3
    assert [lv.stored_or_default(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 3, 4, 5]


def test_the_global_limit_caps_the_customers_number():
    assert lv.limit(None) == lv.limit(0) == lv.limit(5) == lv.limit(125) == 5
    assert lv.limit(2) == 2 and lv.limit(2.9) == 2 and lv.limit(1) == 1 and lv.limit(0.5) == 1  # a ceiling below 1 is read as the lowest usable limit, never as off
    assert lv.account_cap(None, 5) == 3 and lv.account_cap(5, 2) == 2 and lv.account_cap(4, 5) == 4 and lv.account_cap(None, 2) == 2
    with pytest.raises(ValueError):
        lv.validate(3, 2)
    with pytest.raises(ValueError):
        lv.validate(True, 5)
    assert lv.validate(2, 2) == 2


def test_the_mode_parser_defaults_to_strict():
    assert [lv.parse_mode(x) for x in (None, "", "strict", "STRICT", "banana", "size-cap", "size_cap ", "SIZE_CAP")] == ["strict"] * 5 + ["strict", "size_cap", "size_cap"]


def test_settings_default_strict_and_read_the_env(monkeypatch):
    from mba_bot.config import load_settings

    monkeypatch.delenv("FUTURES_LEVERAGE_MODE", raising=False)
    assert load_settings().futures_leverage_mode == "strict"
    assert {f.name: f.default for f in dataclasses.fields(type(load_settings()))}["futures_leverage_mode"] == "strict"
    monkeypatch.setenv("FUTURES_LEVERAGE_MODE", "size_cap")
    assert load_settings().futures_leverage_mode == "size_cap"


# ------------------------------------------------------------------ strict: exactly as on main
def test_strict_ignores_the_customers_number(db, monkeypatch):
    sizes = []
    for cap in (None, 1, 5):
        init_db(f"sqlite:///{db}/strict_{cap}.db")
        notional, refused, *_ = entry(monkeypatch, binance=3, cap=cap, mode="strict")
        assert refused == []
        sizes.append(round(notional[0], 2))
    assert len(set(sizes)) == 1 and sizes[0] <= 3 * CAPITAL + 1  # margin-bound at Binance's 3x whatever the customer chose


def test_strict_refuses_above_the_global_limit_as_before(db, monkeypatch):
    notional, refused, *_ = entry(monkeypatch, binance=20, cap=3, mode="strict")
    assert notional == [] and "futures_leverage_above_ceiling" in reasons() and "above the limit of 5x" in refused[0]


def test_default_mode_is_strict_and_unchanged(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, futures_max_leverage=5.0, protection_precheck_mode="enforce", atr_stop_multiplier=0.02)
    assert st.futures_leverage_mode == "strict"


# ------------------------------------------------------------------ size_cap
@pytest.mark.parametrize("binance,cap,expected_x", [(2, 3, 2.0), (3, 3, 3.0), (20, 3, 3.0), (20, 1, 1.0), (2, 1, 1.0)])
def test_size_cap_sizes_to_the_smaller_of_binance_and_the_customer(db, monkeypatch, binance, cap, expected_x):
    notional, refused, *_ = entry(monkeypatch, binance=binance, cap=cap)
    assert refused == [] and len(notional) == 1  # a high Binance number never refuses
    assert abs(notional[0] - expected_x * CAPITAL) < 0.01 * CAPITAL  # one lot step of rounding at most


def test_size_cap_empty_column_reads_as_three(db, monkeypatch):
    notional, *_ = entry(monkeypatch, binance=20, cap=None)
    assert notional[0] <= 3 * CAPITAL + 1 and notional[0] > 2.5 * CAPITAL


def test_size_cap_never_exceeds_the_global_limit(db, monkeypatch):
    notional, *_ = entry(monkeypatch, binance=20, cap=5, global_=2.0)
    assert notional[0] <= 2 * CAPITAL + 1


def test_size_cap_with_the_global_limit_off_still_caps_to_the_customers_number(db, monkeypatch):
    notional, refused, *_ = entry(monkeypatch, binance=20, cap=3, global_=0.0)
    assert refused == [] and notional[0] <= 3 * CAPITAL + 1


def test_liquidation_check_judges_the_real_binance_leverage_not_the_customers_number(db, monkeypatch):
    """Binance 20x, customer 3x. (a) A tight stop: opens, position value <= 3 x capital. (b) A stop 7.8% from entry sits beyond the safe distance for 20x
    (about 3.4% of entry) though it would be far inside liquidation at 3x: still refused by the pre-entry check."""
    notional, refused, *_ = entry(monkeypatch, binance=20, cap=3)
    assert refused == [] and notional[0] <= 3 * CAPITAL + 1
    init_db(f"sqlite:///{db}/second.db")
    notional2, refused2, *_ = entry(monkeypatch, binance=20, cap=3, mult=0.3)
    assert notional2 == [] and "stop_beyond_safe_liquidation_distance" in reasons() and "20x" in refused2[0]
    assert "to a lower number allows trading it again" in refused2[0] and "10x" not in refused2[0]


@pytest.mark.parametrize("margin", ["cross", None])
def test_cross_or_unreadable_margin_mode_is_refused_with_the_fixed_message(db, monkeypatch, margin):
    notional, refused, st, client = entry(monkeypatch, binance=20, margin=margin, cap=3)
    assert notional == [] and client.orders == {}
    assert "futures_margin_not_isolated" in reasons()
    assert refused and lv.MSG_NOT_ISOLATED.format(pair="BTC/USDT") in refused[0]


def test_unreadable_leverage_is_still_refused(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, futures_leverage_mode="size_cap", futures_max_leverage=5.0, protection_precheck_mode="enforce", atr_stop_multiplier=0.02)
    client = futures_client(leverage_by_symbol={}, margin_mode="isolated")  # no leverage reported
    st = dataclasses.replace(st, trade_telemetry=True)
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures", allocated_capital=CAPITAL)
    run_cycle(aid, st)
    assert all_trades(aid) == [] and "leverage_unreadable" in reasons()


def test_the_bot_never_writes_leverage_or_margin_mode(db, monkeypatch):
    _, _, _, client = entry(monkeypatch, binance=20, cap=3)
    assert not any(c[0] in ("set_leverage", "set_margin_mode", "setLeverage", "setMarginMode") for c in client.calls)


# ------------------------------------------------------------------ fallback to strict
@pytest.mark.parametrize("precheck", ["observe", "off"])
def test_size_cap_without_enforced_precheck_behaves_as_strict_and_says_so_once(db, monkeypatch, caplog, precheck):
    from mba_bot import protection_guard

    protection_guard.reset_for_tests()  # the once-an-hour notice is remembered in memory
    caplog.set_level(logging.WARNING, logger="mba_bot.trader")
    notional, refused, *_ = entry(monkeypatch, binance=20, cap=3, precheck=precheck)
    assert notional == [] and "futures_leverage_above_ceiling" in reasons()  # strict: Binance 20x is above the global limit of 5
    lines = [r.getMessage() for r in caplog.records if "needs PROTECTION_PRECHECK_MODE=enforce" in r.getMessage()]
    assert len(lines) == 1 and precheck in lines[0]


def test_the_fallback_is_strict_not_a_silent_size_cap(db, monkeypatch):
    # global limit off + observe: strict does no leverage check and sizes to the REAL Binance leverage, so the customer's 3x is NOT applied (and no liquidation check runs)
    notional, refused, *_ = entry(monkeypatch, binance=20, cap=3, global_=0.0, precheck="observe")
    assert refused == [] and notional[0] > 3 * CAPITAL


def test_the_fallback_notice_is_not_repeated_every_cycle(db, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="mba_bot.trader")
    st = make_settings(max_notional_multiplier=100.0, futures_leverage_mode="size_cap", futures_max_leverage=5.0, protection_precheck_mode="observe", atr_stop_multiplier=0.02)
    client = futures_client(leverage_by_symbol={"BTC/USDT": 3.0}, margin_mode="isolated")
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures", allocated_capital=CAPITAL)
    for _ in range(3):
        run_cycle(aid, st)
    assert len([r for r in caplog.records if "needs PROTECTION_PRECHECK_MODE=enforce" in r.getMessage()]) <= 1


# ------------------------------------------------------------------ open trade and Spot are unaffected
def test_a_change_never_touches_an_open_trade(db, monkeypatch):
    base = make_settings(max_notional_multiplier=100.0, futures_leverage_mode="size_cap", futures_max_leverage=5.0, protection_precheck_mode="enforce")
    client, st, aid = open_protected(monkeypatch, market="futures", settings=base)
    before = all_trades(aid)[0]
    snapshot = (before.status, before.quantity, before.entry_price, before.stop_loss_price)
    orders = {k: dict(v) for k, v in client.orders.items()}
    with session_scope() as s:
        s.get(TradingAccount, aid).max_leverage = 1
    run_cycle(aid, st)
    after = all_trades(aid)[0]
    assert (after.status, after.quantity, after.entry_price, after.stop_loss_price) == snapshot and {k: dict(v) for k, v in client.orders.items()} == orders


def test_spot_ignores_the_setting(db, monkeypatch):
    sizes = []
    for cap in (1, 5):
        init_db(f"sqlite:///{db}/spot_{cap}.db")
        st = make_settings(max_notional_multiplier=100.0, futures_leverage_mode="size_cap", futures_max_leverage=5.0, protection_precheck_mode="enforce")
        client = spot_client()
        install(monkeypatch, client)
        aid = new_account(st, market_type="spot")
        with session_scope() as s:
            s.get(TradingAccount, aid).max_leverage = cap
        run_cycle(aid, st)
        t = all_trades(aid)
        assert len(t) == 1 and not any(c[0] == "fetch_leverage" for c in client.calls)
        sizes.append(t[0].quantity)
    assert sizes[0] == sizes[1]


# ------------------------------------------------------------------ migration, docs, no special cases
def _cfg(monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    cfg = _cfg(monkeypatch, path)
    command.upgrade(cfg, "a8d2f6c1e3b7")
    cols = lambda: {c["name"] for c in inspect(create_engine(f"sqlite:///{path}")).get_columns("trading_accounts")}
    assert "max_leverage" not in cols()
    before = cols()
    command.upgrade(cfg, "b9e3d7a2c5f1")  # this revision only (later ones add other columns)
    assert cols() == before | {"max_leverage"}
    command.upgrade(cfg, "b9e3d7a2c5f1")  # idempotent
    command.downgrade(cfg, "a8d2f6c1e3b7")
    assert cols() == before
    command.upgrade(cfg, "head")
    assert "max_leverage" in cols()


def test_the_head_is_the_new_revision():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_revision("b9e3d7a2c5f1").down_revision == "a8d2f6c1e3b7" and script.get_heads() == ["a1d6c3e8b5f7"]  # (the head moved on with the daily loss limit)


def test_the_help_file_and_the_code_agree_on_every_message():
    text = (ROOT / "docs" / "customer" / "leverage_help.md").read_text()
    assert lv.MSG_NOT_ISOLATED in text
    for needle in ("21.60 x 3 = 64.80 USDT", "21.60 x 2 = 43.20 USDT", "UNVERIFIED", "never changes", "lose all the money set aside"):
        assert needle in text, needle


def test_the_stop_refusal_says_a_lower_number_and_gives_no_example_number():
    from mba_bot import risk, trader

    check = risk.StopViability(False, risk.STOP_UNVIABLE, stop_distance_frac=0.078, max_allowed_stop_frac=0.034)
    account = TradingAccount(pair="BTC/USDT")
    message = trader._precheck_message(account, check, 20.0)
    assert "Changing the leverage for this pair on Binance to a lower number allows trading it again." in message
    assert "10x" not in message and "for example" not in message
    assert "your Binance leverage (20x) puts liquidation too close" in message and "about 7.80% from entry" in message and "about 3.40% is safe" in message
    help_text = (ROOT / "docs" / "customer" / "leverage_help.md").read_text()
    assert "Changing the leverage for this pair on Binance to a lower number allows trading it again." in help_text
    assert "for example to 10x" not in help_text and "(for example to" not in help_text


def test_the_liquidation_paragraph_says_estimate_not_a_price_binance_reports():
    text = (ROOT / "docs" / "customer" / "leverage_help.md").read_text()
    paragraph = text[text.index("**Please read this warning.**"):text.index("## 2. Margin mode")]
    assert "estimate" in paragraph and "maintenance-margin tiers" in paragraph and "not a price Binance gives the bot" in paragraph
    assert "Binance reports" not in text and "liquidation price that Binance" not in text


def test_the_leverage_setting_is_dashboard_only_in_every_doc():
    import re

    for name in ("customer/leverage_help.md", "safety/customer_leverage.md"):
        text = (ROOT / "docs" / name).read_text()
        assert not re.search(r"(?<![\w.])/(leverage|help)(?!\w)", text), name  # no command (the path docs/customer/leverage_help.md is not one)
    assert "no Telegram command" in (ROOT / "docs" / "safety" / "customer_leverage.md").read_text()
    assert "leverage" not in (ROOT / "src" / "mba_bot" / "api" / "routers" / "telegram.py").read_text().lower()
    assert "/leverage" not in (ROOT / ".env.example").read_text()


def test_no_special_cases_for_any_account_in_the_new_code():
    import re

    for name in ("leverage.py",):
        body = (ROOT / "src" / "mba_bot" / name).read_text().lower()
        assert not re.search(r"owner|gideon|gidoty|@gmail|account_id ==|\.id ==", body)
    trader = (ROOT / "src" / "mba_bot" / "trader.py").read_text()
    block = trader[trader.index("def _leverage_mode"):trader.index("# One equity reading")]
    assert not re.search(r"owner|gideon|gidoty|@", block.lower())


def test_a_very_high_binance_number_is_judged_by_the_liquidation_check_not_waved_through(db, monkeypatch):
    # Binance 125x: liquidation is about 0.4% from entry. The customer's 3x caps the size but must not hide that from the check: the 0.5% stop is refused.
    notional, refused, *_ = entry(monkeypatch, binance=125, cap=3)
    assert notional == [] and "stop_beyond_safe_liquidation_distance" in reasons() and "125x" in refused[0]
