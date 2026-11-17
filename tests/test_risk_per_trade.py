"""B1c: risk per trade up to 40 percent. The pure limits (range, bands, acknowledgments, total loss threshold, open-risk cap), the config clamp, and the account API:
a value above the limit is REFUSED (never silently lowered), a value above 2 or from 10 needs its recorded acknowledgment, lowering needs none.

PROVES: boundaries 0.1, 2, 2.01, 9.99, 10, 40, 40.01; RISK_PCT_MAX clamps to 40; the server (not only the screen) asks for the acknowledgments; the effective-limits table in
docs/safety/risk_per_trade.md equals what the code computes.
DOES NOT PROVE: anything about the screens in a browser, or about Binance."""
from __future__ import annotations

import dataclasses
import os
import re
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_risk_per_trade.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot import multi_trade as mt  # noqa: E402
from mba_bot import risk_limits as rl  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.deps import get_settings  # noqa: E402
from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, LegalAcceptance  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_counter = iter(range(1, 99_999))


# ------------------------------------------------------------------ pure rules
@pytest.mark.parametrize("value,limit,ok", [(0.1, 40, True), (0.09, 40, False), (1, 2, True), (2, 2, True), (2.01, 2, False), (2.01, 40, True), (9.99, 40, True), (10, 40, True), (40, 40, True),
                                            (40.01, 40, False), (5, 2, False), (float("nan"), 40, False), (float("inf"), 40, False), (True, 40, False), ("5", 40, False), (None, 40, False)])
def test_range_boundaries(value, limit, ok):
    assert (rl.validate_range(value, limit) is None) is ok


def test_the_refusal_text_is_plain_and_says_nothing_was_changed():
    assert rl.validate_range(5, 2.0) == "Risk per trade can be at most 2% on this service. Nothing was changed."
    assert rl.validate_range(0.05, 2.0) == "Risk per trade must be at least 0.1%."


@pytest.mark.parametrize("pct,band", [(0.1, "recommended"), (1, "recommended"), (2, "recommended"), (2.0000000001, "recommended"), (2.01, "high"), (5, "high"), (9.99, "high"), (10, "very_high"), (40, "very_high"),
                                      (float("nan"), "very_high")])
def test_bands(pct, band):
    assert rl.band(pct) == band


@pytest.mark.parametrize("pct,needed", [(0.1, ()), (2, ()), (2.01, ("risk_ack_high",)), (9.99, ("risk_ack_high",)), (10, ("risk_ack_high", "risk_ack_very_high")), (40, ("risk_ack_high", "risk_ack_very_high"))])
def test_required_acknowledgments(pct, needed):
    assert rl.required_acknowledgments(pct) == needed


@pytest.mark.parametrize("raw,expected", [("100", 40.0), ("40", 40.0), ("40.5", 40.0), ("2", 2.0), ("2.5", 2.5), ("0.01", 0.1), ("-3", 0.1), ("nan", 2.0)])
def test_the_env_value_is_clamped_to_0_1_through_40(monkeypatch, raw, expected):
    monkeypatch.setenv("RISK_PCT_MAX", raw)
    assert load_settings().risk_pct_max == expected


@pytest.mark.parametrize("raw", ["abc", ""])
def test_a_garbage_env_value_fails_loudly_at_start_like_every_numeric_setting(monkeypatch, raw):
    monkeypatch.setenv("RISK_PCT_MAX", raw)
    with pytest.raises(ValueError):
        load_settings()


def test_the_default_limit_is_two(monkeypatch):
    monkeypatch.delenv("RISK_PCT_MAX", raising=False)
    assert load_settings().risk_pct_max == 2.0 and load_settings().account_total_loss_stop is False


def test_total_loss_threshold_is_one_fixed_number_for_every_risk_setting():
    t = rl.total_loss_threshold
    assert rl.TOTAL_STOP_PCT == 50.0
    assert {t(x) for x in (0.1, 1, 2, 3.33, 5, 10, 20, 26.7, 40, float("nan"), 0, -1, None)} == {50.0}
    assert t() == 50.0 and not hasattr(rl, "TOTAL_STOP_MULTIPLE") and not hasattr(rl, "TOTAL_STOP_CAP_PCT") and not hasattr(rl, "TOTAL_STOP_MIN_PCT")


def test_total_loss_hit_boundaries_and_fail_closed():
    import math

    hit = lambda eq, risk=1.0, ref=1000.0: rl.total_loss_hit(reference_equity=ref, equity=eq, risk_pct=risk)
    assert hit(500.0) is not None and abs(hit(500.0) - 50.0) < 1e-9 and hit(500.01) is None  # at 50 percent: a hit
    for risk in (0.1, 1.0, 5.0, 40.0):  # the same line for every risk setting
        assert hit(500.0, risk=risk) is not None and hit(500.1, risk=risk) is None
    assert hit(900.0) is None and hit(600.0, risk=40.0) is None  # a 10 or 40 percent fall no longer stops anything
    for bad_equity in (float("nan"), None, "x", float("inf")):
        assert math.isnan(hit(bad_equity)) if not isinstance(bad_equity, float) or math.isnan(bad_equity) else hit(bad_equity) is not None  # a bad reading is a hit (nan)
    for bad_ref in (0, -5, None, float("nan"), "x"):
        result = hit(500.0, ref=bad_ref)
        assert result is not None and math.isnan(result)  # a bad reference is a hit too


def test_open_risk_cap_values():
    cap = mt.open_risk_cap
    assert [cap(2.0, 1.0, n) for n in (1, 2, 3)] == [2.0, 2.0, 3.0]
    assert cap(2.0, 40.0, 3) == 100.0 and cap(100.0, 40.0, 3) == 100.0 and cap(0.0, 40.0, 3) == 0.0


EXPECTED_TABLE = {
    1: (50.0, [2.0, 2.0, 3.0]), 2: (50.0, [2.0, 4.0, 6.0]), 5: (50.0, [5.0, 10.0, 15.0]),
    10: (50.0, [10.0, 20.0, 30.0]), 20: (50.0, [20.0, 40.0, 60.0]), 40: (50.0, [40.0, 80.0, 100.0]),
}


def test_the_effective_limits_table_equals_what_the_code_computes_and_the_doc():
    doc = (ROOT / "docs" / "safety" / "risk_per_trade.md").read_text()
    for risk, (stop, caps) in EXPECTED_TABLE.items():
        assert rl.total_loss_threshold(risk) == stop
        assert [mt.open_risk_cap(2.0, risk, n) for n in (1, 2, 3)] == caps
        row = next(line for line in doc.splitlines() if line.startswith(f"| {risk}% |"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[1] == "set by you"  # the daily limit is the customer's own amount in every row
        assert float(cells[2].split("%")[0]) == stop
        assert [float(c.split("%")[0]) for c in cells[3:6]] == caps


# ------------------------------------------------------------------ the account API
def _signup(client: TestClient, *, risk_pct=None, extra=None) -> str:
    n = next(_counter)
    email = f"risk{n}@example.com"
    assert client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True}).status_code == 201
    body = {"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"}
    if risk_pct is not None:
        body["risk_pct"] = risk_pct
    body.update(extra or {})
    r = client.post("/api/account", json=body)
    assert r.status_code in (201, 422), r.text
    return email


def _acks(email: str) -> set[str]:
    with session_scope() as s:
        cid = s.query(Customer).filter(Customer.email == email).one().id
        return {a.document_type for a in s.query(LegalAcceptance).filter(LegalAcceptance.customer_id == cid).all()} & {rl.ACK_HIGH, rl.ACK_VERY_HIGH}


@pytest.fixture
def limit():
    def set_limit(value):
        app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), risk_pct_max=float(value))

    yield set_limit
    app.dependency_overrides.pop(get_settings, None)


def test_the_default_limit_refuses_above_two_and_changes_nothing():
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.patch("/api/account", json={"risk_pct": 1.5}).status_code == 200
        for bad in (2.01, 5, 40, 0.05, 0):
            r = client.patch("/api/account", json={"risk_pct": bad})
            assert r.status_code == 422, bad
        assert client.get("/api/account").json()["risk_pct"] == 1.5  # never silently lowered to 2: nothing changed
        assert "at most 2%" in client.patch("/api/account", json={"risk_pct": 5}).json()["detail"]
        assert client.patch("/api/account", json={"risk_pct": 2}).json()["risk_pct"] == 2.0
        assert client.get("/api/account").json()["risk_pct_limit"] == 2.0


def test_create_above_the_limit_is_refused_too():
    with TestClient(app, base_url="https://testserver") as client:
        n = next(_counter)
        client.post("/api/auth/signup", json={"email": f"risk{n}@example.com", "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
        r = client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "risk_pct": 5})
        assert r.status_code == 422 and "at most 2%" in r.json()["detail"]
        assert client.get("/api/account").status_code == 404  # no account was created


def test_the_server_asks_for_the_acknowledgments(limit):
    limit(40)
    with TestClient(app, base_url="https://testserver") as client:
        email = _signup(client)
        patch = lambda body: client.patch("/api/account", json=body)
        assert patch({"risk_pct": 2}).status_code == 200 and _acks(email) == set()
        r = patch({"risk_pct": 2.01})
        assert r.status_code == 422 and "above 2%" in r.json()["detail"] and client.get("/api/account").json()["risk_pct"] == 2.0 and _acks(email) == set()
        assert patch({"risk_pct": 5, "acknowledge_very_high_risk": True}).status_code == 422  # the wrong acknowledgment does not count
        assert patch({"risk_pct": 5, "acknowledge_high_risk": True}).status_code == 200
        assert client.get("/api/account").json()["risk_pct"] == 5.0 and _acks(email) == {rl.ACK_HIGH}
        assert patch({"risk_pct": 9.99}).status_code == 200  # on file: no second tick needed within the band
        r = patch({"risk_pct": 10})
        assert r.status_code == 422 and "10% or more" in r.json()["detail"] and client.get("/api/account").json()["risk_pct"] == 9.99
        assert patch({"risk_pct": 10, "acknowledge_very_high_risk": True}).status_code == 200 and _acks(email) == {rl.ACK_HIGH, rl.ACK_VERY_HIGH}
        assert patch({"risk_pct": 40}).status_code == 200
        assert patch({"risk_pct": 40.01, "acknowledge_high_risk": True, "acknowledge_very_high_risk": True}).status_code == 422
        assert client.get("/api/account").json()["risk_pct"] == 40.0
        assert patch({"risk_pct": 1}).status_code == 200  # lowering needs nothing
        assert patch({"risk_pct": 0.1}).status_code == 200 and client.get("/api/account").json()["risk_pct"] == 0.1


def test_a_first_request_straight_to_ten_needs_both(limit):
    limit(40)
    with TestClient(app, base_url="https://testserver") as client:
        email = _signup(client)
        assert client.patch("/api/account", json={"risk_pct": 10, "acknowledge_high_risk": True}).status_code == 422
        assert _acks(email) == set()  # a refused request records nothing
        assert client.patch("/api/account", json={"risk_pct": 10, "acknowledge_high_risk": True, "acknowledge_very_high_risk": True}).status_code == 200
        assert _acks(email) == {rl.ACK_HIGH, rl.ACK_VERY_HIGH}


def test_creating_an_account_above_two_needs_the_acknowledgment_too(limit):
    limit(40)
    with TestClient(app, base_url="https://testserver") as client:
        n = next(_counter)
        email = f"risk{n}@example.com"
        client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
        base = {"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "risk_pct": 3}
        assert client.post("/api/account", json=base).status_code == 422 and client.get("/api/account").status_code == 404
        assert client.post("/api/account", json={**base, "acknowledge_high_risk": True}).status_code == 201
        assert client.get("/api/account").json()["risk_pct"] == 3.0 and _acks(email) == {rl.ACK_HIGH}


def test_a_refused_request_does_not_change_other_fields_either(limit):
    limit(40)
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.patch("/api/account", json={"pair": "ETH/USDT", "risk_pct": 5}).status_code == 422
        assert client.get("/api/account").json()["pair"] == "BTC/USDT"


def test_the_limit_endpoint_and_account_out_carry_the_limit(limit):
    limit(25)
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.get("/api/account").json()["risk_pct_limit"] == 25.0
        assert client.get("/api/account/risk-limits").json() == {"risk_pct_limit": 25.0, "recommended_max": 2.0, "very_high_from": 10.0}
        assert client.patch("/api/account", json={"risk_pct": 25.01, "acknowledge_high_risk": True, "acknowledge_very_high_risk": True}).status_code == 422


def test_the_total_loss_reset_needs_a_confirmation():
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.post("/api/account/total-loss-reset", json={"confirm": False}).status_code == 422
        assert client.post("/api/account/total-loss-reset", json={}).status_code == 422
        r = client.post("/api/account/total-loss-reset", json={"confirm": True})
        assert r.status_code == 200 and r.json()["total_loss_stop_active"] is False and r.json()["total_loss_stop_threshold_pct"] == 50.0


# ------------------------------------------------------------------ text agrees everywhere
def test_every_screen_and_doc_agrees_with_the_new_range():
    landing = (ROOT / "frontend/src/pages/Landing.jsx").read_text()
    howto = (ROOT / "frontend/src/howTo.js").read_text()
    onboarding = (ROOT / "frontend/src/pages/Onboarding.jsx").read_text()
    settings = (ROOT / "frontend/src/pages/BotSettings.jsx").read_text()
    assert "Set 0.1% to 2%" not in landing and "0.1% and 2%" not in howto
    assert "2% or less" in landing and "2% or less" in howto
    for name, text in (("onboarding", onboarding), ("settings", settings)):
        assert 'max="2"' not in text and "RiskPctField" in text, name
    assert "risk_pct_limit" in settings and "/account/risk-limits" in onboarding
    assert "acknowledge_high_risk" in settings and "acknowledge_high_risk" in onboarding
    field = (ROOT / "frontend/src/components/RiskPctField.jsx").read_text()
    assert "aria-hidden" in field and "BAND_INFO" in field and "role=\"status\"" in field


# ------------------------------------------------------------------ where the open-risk cap applies today (strategy guards, paper multi-trade)
@pytest.fixture
def gdb(tmp_path):
    from mba_bot.db import init_db

    init_db(f"sqlite:///{tmp_path}/guards.db")


def _guard_hits(settings_, risk, *, is_paper, open_trades):
    from datetime import datetime, timezone

    from test_donchian_integration import paper_account

    from mba_bot import strategy_guards as g
    from mba_bot.models import Trade, TradingAccount
    from mba_bot.strategy import SignalType, StrategySignal

    aid = paper_account(settings_, email=f"guard{next(_counter)}@example.com")
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.get(TradingAccount, aid).risk_pct = risk
        for i in range(open_trades):
            s.add(Trade(account_id=aid, side="long", is_paper=is_paper, entry_price=90.0, quantity=1.0, stop_loss_price=89.0, status="open", pair=("ETH/USDT", "BNB/USDT", "XRP/USDT")[i],
                        strategy_id="ema_cross_atr_v1", opened_at=now))
    ema = StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 100.0, 99.0, 1.0, 0.05, gap_atr=1.0)
    with session_scope() as s:
        return [h.code for h in g.evaluate(s, s.get(TradingAccount, aid), settings_, signal=ema, is_paper=is_paper, now=now)]


def test_a_single_live_trade_is_not_refused_by_the_cap_because_of_its_own_risk(gdb):
    from test_donchian_integration import settings as dsettings

    from mba_bot import strategy_guards as g

    st = dsettings(max_open_positions=3, max_total_risk_pct=2.0)
    # risk 10% on one live position: the old comparison (10 > 2.0) refused every entry; the cap is now max(2.0, 10 x 1) = 10
    assert g.R_PORTFOLIO_CAP not in _guard_hits(st, 10.0, is_paper=False, open_trades=0)
    assert g.R_PORTFOLIO_CAP not in _guard_hits(st, 40.0, is_paper=False, open_trades=0)


def test_the_paper_cap_counts_the_positions_the_account_may_hold(gdb):
    from test_donchian_integration import settings as dsettings

    from mba_bot import strategy_guards as g

    st = dsettings(max_open_positions=3, max_total_risk_pct=2.0)
    # two open paper trades (2.0 nominal) plus a new one at 1.0 is 3.0: over the old fixed 2.0, within max(2.0, 1 x 3) = 3.0
    assert g.R_PORTFOLIO_CAP not in _guard_hits(st, 1.0, is_paper=True, open_trades=2)
    # a raised MAX_TOTAL_RISK_PCT still wins when it is the larger number, and the cap still refuses above it
    st_low = dsettings(max_open_positions=3, max_total_risk_pct=2.0)
    assert g.R_PORTFOLIO_CAP in _guard_hits(st_low, 1.0, is_paper=True, open_trades=3)  # three open (3.0) plus one more (4.0) over 3.0


def test_the_owner_steps_use_the_safe_order_and_the_env_example_lists_the_new_switch():
    text = (ROOT / "docs" / "safety" / "risk_per_trade.md").read_text()
    steps = text[text.index("## Owner steps"):]
    order = ["restart_gate", "Stop the service", "Back up", "alembic upgrade head", "Deploy the code", "Start the service"]
    pos = [steps.index(k) for k in order]
    assert pos == sorted(pos) and "Rollback" in steps and "c1f4a8d2e6b3" in steps and "NOT verified" in text
    assert "one stop-out can already exceed it" in (ROOT / "docs" / "safety" / "account_daily_loss.md").read_text()
    env = (ROOT / ".env.example").read_text()
    assert "# ACCOUNT_TOTAL_LOSS_STOP=0" in env and "RISK_PCT_MAX=2.0" in env
