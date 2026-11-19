"""Setup advisor (docs/safety/setup_advisor.md). SETUP_ADVISOR is off by default and the advisor only informs.

PROVES: with the switch off the route does not exist (404) and nothing else changes; a POST changes nothing on the account; the advisor says "not possible" for the account settings EXACTLY when the real
PATCH /api/account refuses them (a grid of risk, acknowledgments, daily limit, slots and Auto Select, each run through both and compared); the pair note says "not possible" exactly when
small_capital.assess says the pair cannot be traded; the open-risk note matches live_multi.check_additional_entry; no exchange call is made; the response has no secret and no account parameter; every
text comes from setup_advice_texts and none gives trading instructions or promises a limit.
DOES NOT PROVE: how the screens look, or anything about Binance (UNVERIFIED: the real exchange minimums and leverage)."""
from __future__ import annotations

import dataclasses
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_setup_advisor.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot import exchange, live_multi, risk_limits, setup_advice as sa, setup_advice_texts as texts, small_capital  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.deps import get_settings  # noqa: E402
from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import TradingAccount  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_counter = iter(range(1, 99_999))


def _override(**kw):
    app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), **kw)


@pytest.fixture(autouse=True)
def _clean():
    yield
    app.dependency_overrides.pop(get_settings, None)


def _signup(client, *, capital=1000.0) -> str:
    n = next(_counter)
    email = f"adv{n}@example.com"
    assert client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True}).status_code == 201
    r = client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"})
    assert r.status_code == 201, r.text
    if capital is not None:
        assert client.patch("/api/account", json={"allocated_capital": capital}).status_code == 200
    return email


def _on(**extra):
    _override(setup_advisor=True, risk_pct_max=40.0, live_multi_position=True, **extra)


def _ids(notes, severity=None):
    return {n["id"] for n in notes if severity is None or n["severity"] == severity}


# ------------------------------------------------------------------------------------------------ switch off
def test_the_switch_defaults_off_and_the_route_does_not_exist_then():
    assert load_settings().setup_advisor is False
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.get("/api/account/advice").status_code == 404
        assert client.post("/api/account/advice", json={"risk_pct": 5}).status_code == 404
        me = client.get("/api/account").json()
        assert not any("advice" in k or "advisor" in k for k in me)  # no new field on the account response


def test_it_needs_a_login_and_an_account():
    with TestClient(app, base_url="https://testserver") as client:
        _on()
        assert client.get("/api/account/advice").status_code in (401, 403)
        client.post("/api/auth/signup", json={"email": f"adv{next(_counter)}@example.com", "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
        assert client.get("/api/account/advice").status_code == 404  # no account yet


# ------------------------------------------------------------------------------------------------ it only informs
def test_a_post_changes_nothing_and_makes_no_exchange_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the advisor must not call the exchange")

    monkeypatch.setattr(exchange, "build_exchange_client", boom)
    with TestClient(app, base_url="https://testserver") as client:
        _on()
        email = _signup(client)
        with session_scope() as s:
            before = {c.name: getattr(a, c.name) for a in [s.query(TradingAccount).join(TradingAccount.customer).filter_by(email=email).one()] for c in TradingAccount.__table__.columns}
        r = client.post("/api/account/advice", json={"risk_pct": 40, "allocated_capital": 5, "daily_loss_limit_usd": 99999, "live_slots": 3, "pair_selection_mode": "auto"})
        assert r.status_code == 200 and client.get("/api/account/advice").status_code == 200
        with session_scope() as s:
            after = {c.name: getattr(a, c.name) for a in [s.query(TradingAccount).join(TradingAccount.customer).filter_by(email=email).one()] for c in TradingAccount.__table__.columns}
        assert before == after


def test_the_response_has_notes_only_no_secret_and_takes_no_account_parameter():
    with TestClient(app, base_url="https://testserver") as client:
        _on()
        _signup(client)
        body = client.get("/api/account/advice").json()
        assert set(body) == {"notes"}
        assert all(set(n) == {"id", "severity", "text", "action", "tab"} for n in body["notes"])
        blob = str(body).lower()
        assert not any(w in blob for w in ("api_key", "secret", "encrypted", "token", "password"))
        # an account id in the query is ignored: it is always the logged-in customer's own account
        assert client.get("/api/account/advice?account_id=someone-else").json() == body


def test_each_customer_gets_notes_for_their_own_account():
    _on()
    with TestClient(app, base_url="https://testserver") as a, TestClient(app, base_url="https://testserver") as b:
        _signup(a, capital=1000.0)
        _signup(b, capital=None)
        assert "no_capital" not in _ids(a.get("/api/account/advice").json()["notes"])
        assert "no_capital" in _ids(b.get("/api/account/advice").json()["notes"])


def test_notes_are_ordered_not_possible_then_warning_then_tip():
    _on()
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        notes = client.post("/api/account/advice", json={"risk_pct": 5, "live_slots": 2, "pair_selection_mode": "auto"}).json()["notes"]
        order = [sa._ORDER[n["severity"]] for n in notes]
        assert order == sorted(order) and notes[0]["severity"] == "not_possible"


# ------------------------------------------------------------------------------------------------ advisor == enforcement (the grid)
def _grid():
    for risk in (0.05, 0.1, 1, 2, 2.01, 5, 9.99, 10, 40, 41):
        for ticks in ((False, False), (True, False), (True, True)):
            yield {"risk_pct": risk, "acknowledge_high_risk": ticks[0], "acknowledge_very_high_risk": ticks[1]}
    for amount in (0.5, 100, 499, 499.99, 500, 501, 1000):
        yield {"daily_loss_limit_usd": amount}
    for slots in (1, 2, 3):
        for mode in (None, "manual", "auto"):
            yield {k: v for k, v in (("live_slots", slots), ("pair_selection_mode", mode)) if v is not None}
    yield {"pair_selection_mode": "auto"}


REFUSAL_IDS = {"risk_refused", "daily_refused", "slots_refused"}


@pytest.mark.parametrize("proposal", list(_grid()), ids=lambda p: ",".join(f"{k}={v}" for k, v in p.items()))
def test_not_possible_exactly_when_the_account_api_refuses(proposal):
    _on()
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client, capital=1000.0)
        advised = _ids(client.post("/api/account/advice", json=proposal).json()["notes"], "not_possible") & REFUSAL_IDS
        refused = client.patch("/api/account", json=proposal).status_code in (409, 422)
        assert bool(advised) is refused, (proposal, advised)


@pytest.mark.parametrize("start_mode,proposal", [("auto", {"live_slots": 2}), ("auto", {"live_slots": 1}), ("manual", {"live_slots": 3})])
def test_the_slot_and_auto_select_rules_agree_from_another_starting_state(start_mode, proposal):
    _on()
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.patch("/api/account", json={"pair_selection_mode": start_mode}).status_code == 200
        advised = "slots_refused" in _ids(client.post("/api/account/advice", json=proposal).json()["notes"], "not_possible")
        assert advised is (client.patch("/api/account", json=proposal).status_code in (409, 422))


def test_with_the_multi_position_switch_off_a_slot_change_is_not_possible_and_the_api_agrees():
    _override(setup_advisor=True)
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert "slots_refused" in _ids(client.post("/api/account/advice", json={"live_slots": 2}).json()["notes"], "not_possible")
        assert client.patch("/api/account", json={"live_slots": 2}).status_code == 409
        assert "slots_refused" not in _ids(client.get("/api/account/advice").json()["notes"])  # nothing proposed: nothing refused


def test_a_value_already_on_file_needs_no_tick_in_the_advisor_either():
    _on()
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.patch("/api/account", json={"risk_pct": 5, "acknowledge_high_risk": True}).status_code == 200
        assert "risk_refused" not in _ids(client.post("/api/account/advice", json={"risk_pct": 4}).json()["notes"])
        assert client.patch("/api/account", json={"risk_pct": 4}).status_code == 200


# ------------------------------------------------------------------------------------------------ pure rules against their enforcement functions
def _facts(**kw):
    base = dict(switch_multi=True, risk_pct_max=40.0, max_notional_multiplier=3.0, futures_max_leverage=5, live_total_risk_cap_pct=3.0, donchian_risk_pct=0.5, rsi_risk_pct=0.5, rsi_live=True,
                donchian_live=True, daily_limit_now=None, reference_value=None, acks_on_file={risk_limits.ACK_HIGH: True, risk_limits.ACK_VERY_HIGH: True}, pair_min_notional={})
    base.update(kw)
    return sa.Facts(**base)


def _acct(**kw):
    base = dict(market_type="futures", pair="BTC/USDT", risk_pct=1.0, allocated_capital=100.0, live_slots=1, pair_selection_mode="manual", max_leverage=3, pair_list=None, extra_strategies=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("min_notional", [5.0, 20.0, 100.0, 300.0, 1000.0])
@pytest.mark.parametrize("capital", [10.0, 33.34, 50.0, 100.0, 1000.0])
@pytest.mark.parametrize("cap", [1, 3, 5])
def test_pair_not_possible_exactly_when_small_capital_says_not_viable(min_notional, capital, cap):
    notes = sa.advise(_acct(allocated_capital=capital, max_leverage=cap), _facts(pair_min_notional={"BTC/USDT": min_notional}))
    v = small_capital.assess(SimpleNamespace(min_notional=min_notional, market_min_qty=0, step=0), None, capital=capital, leverage_cap=small_capital.leverage_cap_for("futures", cap))
    ids = {(n["id"], n["severity"]) for n in notes}
    assert (("pair_viability", "not_possible") in ids) is (v.viable is False)
    assert (("pair_viability", "tip") in ids) is (v.viable is True)


def test_an_unknown_exchange_minimum_is_never_called_not_possible():
    notes = sa.advise(_acct(), _facts(pair_min_notional={}))
    assert "pair_unknown" in _ids(notes) and "pair_viability" not in _ids(notes)


@pytest.mark.parametrize("slots", [2, 3])
@pytest.mark.parametrize("risk", [0.5, 1.0, 1.5, 2.0, 5.0])
@pytest.mark.parametrize("cap_pct", [0.0, 3.0, 6.0, 100.0])
@pytest.mark.parametrize("daily", [None, 1.5, 5.0, 50.0])
def test_the_open_risk_note_matches_the_bots_own_check(slots, risk, cap_pct, daily):
    acct = _acct(risk_pct=risk, live_slots=slots, allocated_capital=100.0, market_type="futures", max_leverage=5, pair_list="ETH/USDT,SOL/USDT")
    f = _facts(live_total_risk_cap_pct=cap_pct, daily_limit_now=daily)
    notes = sa.advise(acct, f)
    size, lev, entry, stop = sa._size_model(capital=100.0, risk_pct=risk, f=f, market_type="futures", cap=5, binance_lev=None)
    trade = SimpleNamespace(quantity=size.quantity, entry_price=entry, stop_loss_price=stop, side="long")
    refused = any(live_multi.check_additional_entry([trade] * (n - 1), new_risk=size.risk_amount, capital=100.0, cap_pct=cap_pct, daily_limit_usd=daily) for n in range(2, slots + 1))
    open_note = next(n for n in notes if n["id"] == "open_risk")
    assert (open_note["severity"] == "warning") is refused and open_note["severity"] != "not_possible"
    _seen.add(refused)


_seen: set = set()


def test_the_open_risk_grid_covers_both_outcomes():
    assert _seen == {True, False}  # runs after the grid above: the grid must contain refusals and non-refusals


def test_the_size_cap_note_appears_only_when_the_cap_really_reduces_the_loss():
    tight = sa.advise(_acct(risk_pct=2.0, max_leverage=1), _facts())  # 1x: a 1% stop would need 2x
    loose = sa.advise(_acct(risk_pct=1.0, max_leverage=5), _facts())
    assert "size_cap_binds" in _ids(tight, "warning") and "size_cap_ok" in _ids(loose, "tip")
    assert "size_cap_binds" not in _ids(loose)


def test_one_trade_above_the_daily_limit_is_a_warning_using_the_daily_limit_function():
    notes = sa.advise(_acct(risk_pct=2.0, allocated_capital=1000.0), _facts(daily_limit_now=10.0, reference_value=1000.0))
    assert "daily_below_one_trade" in _ids(notes, "warning")
    notes = sa.advise(_acct(risk_pct=1.0, allocated_capital=1000.0), _facts(daily_limit_now=100.0, reference_value=1000.0))
    assert "daily_below_one_trade" not in _ids(notes) and "daily_covers_n_trades" in _ids(notes)


def test_slots_need_pairs_and_the_note_names_the_count():
    notes = sa.advise(_acct(live_slots=3, pair_list=None), _facts())
    assert "slots_pairs" in _ids(notes, "warning")
    assert "slots_pairs" not in _ids(sa.advise(_acct(live_slots=3, pair_list="ETH/USDT,SOL/USDT"), _facts()))
    assert not (_ids(sa.advise(_acct(live_slots=3), _facts(switch_multi=False))) & {"slots_pairs", "slots_capital", "open_risk"})  # switch off: no multi-position notes


def test_strategies_follow_the_priority_and_use_the_smaller_risk():
    acct = _acct(risk_pct=2.0, extra_strategies="rsi_mr_v1,donchian_breakout_v1")
    note = next(n for n in sa.advise(acct, _facts()) if n["id"] == "strategies")
    assert note["text"].index("Donchian") < note["text"].index("EMA") < note["text"].index("RSI")
    assert "Donchian 0.5%" in note["text"] and "EMA 2%" in note["text"] and "RSI 0.5%" in note["text"]
    assert "strategies_one_slot" in _ids(sa.advise(acct, _facts()))
    off = sa.advise(acct, _facts(donchian_live=False, rsi_live=False))
    assert "strategies_not_running" in _ids(off)


def test_leverage_uses_binances_number_only_when_it_is_for_the_same_pair():
    same = sa.advise(_acct(max_leverage=5), _facts(binance_leverage=2.0, binance_pair="BTC/USDT", binance_margin_mode="isolated"))
    other = sa.advise(_acct(max_leverage=5), _facts(binance_leverage=2.0, binance_pair="ETH/USDT", binance_margin_mode="cross"))
    assert "2x" in next(n for n in same if n["id"] == "leverage")["text"] and "margin_not_isolated" not in _ids(same)
    assert "Binance's own leverage" not in next(n for n in other if n["id"] == "leverage")["text"] and "margin_not_isolated" not in _ids(other)
    cross = sa.advise(_acct(), _facts(binance_leverage=3.0, binance_pair="BTC/USDT", binance_margin_mode="cross"))
    assert "margin_not_isolated" in _ids(cross, "warning")


def test_the_risk_bands_and_the_deposit_note():
    for risk, sev in ((1.0, "tip"), (2.0, "tip"), (2.5, "warning"), (10.0, "warning")):
        note = next(n for n in sa.advise(_acct(risk_pct=risk), _facts()) if n["id"] == "risk_band")
        assert note["severity"] == sev
    assert sum(1 for n in sa.advise(_acct(), _facts()) if n["id"] == "money_moves") == 1


# ------------------------------------------------------------------------------------------------ wording
FORBIDDEN = re.compile(r"\b(you should (buy|sell|trade)|buy now|sell now|will (make|earn|profit)|guarantee|guaranteed|cannot lose|will not lose|safe to|we recommend|we advise|best time)\b", re.I)


def test_no_text_tells_the_customer_what_to_trade_predicts_or_promises():
    for key, text in texts.TEXTS.items():
        assert not FORBIDDEN.search(text), key
    assert "{" not in texts.as_markdown().split("| id | text |", 1)[0]


def test_every_note_text_comes_from_the_texts_file_or_an_enforcement_rule():
    src = (ROOT / "src" / "mba_bot" / "setup_advice.py").read_text()
    # the only prose literals allowed in the module are the two action lines below; everything else is render(key) or a rule's own text
    literals = re.findall(r'Note\("[a-z_]+", [A-Z_]+, (?:problem|refusal\[1\]|render\()', src)
    assert len(literals) >= 15
    assert not re.search(r'Note\("[a-z_]+", [A-Z_]+, f?"', src)


def test_the_advisor_never_writes():
    src = (ROOT / "src" / "mba_bot" / "setup_advice.py").read_text()
    assert not re.search(r"\.commit\(|\.flush\(|session|requests\.|httpx|build_exchange_client|fetch_", src)
