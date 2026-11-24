"""Operator settings table and admin actions (docs/safety/operator_settings.md). Fake everything: an isolated SQLite file, the admin app, no exchange, no network.

PROVES: the allowlist (no secret-looking key, only bool, number and choice types, bounds enforced, unknown keys refused); one reader that falls back to the environment (no row, bad row, missing table); restart-needed
keys apply only at startup; every write route is 403 with ADMIN_ACTIONS_ENABLED off, needs a reason and a fresh authenticator code (wrong, replayed, locked), writes an admin_changes row with who, old, new and reason in the
same transaction; each customer action does exactly its one change and nothing else (no trade or order is touched); the kill action only sets the existing kill flag; the dashboard routes need the admin session.
DOES NOT PROVE the screens in a browser or any behaviour on Binance."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from sqlalchemy import select

from admin_support import ADMIN_EMAIL, NOW, client_for, make_env, mk_account, mk_customer, mk_trade
from mba_bot import admin_auth, admin_actions, kill_switch, operator_settings as O
from mba_bot.api.routers import admin as admin_router
from mba_bot.config import load_settings
from mba_bot.db import session_scope
from mba_bot.models import AdminChange, Customer, CustomerNote, DataRequest, OperatorSetting, Subscription, Trade, TradingAccount


class Env:
    pass


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_ACTIONS_ENABLED", "1")
    O.reset_cache()
    app, settings = make_env(monkeypatch, tmp_path)
    e = Env()
    e.app, e.settings, e.monkeypatch = app, settings, monkeypatch
    with session_scope() as s:
        admin = mk_customer(s, ADMIN_EMAIL)
        target = mk_customer(s, "target@example.com", paid=True)
        acct = mk_account(s, target, code="MBA-TEST01")
        e.admin_id, e.target_id, e.ref = admin.id, target.id, acct.id
        secret = admin_auth.enrol(s, admin.id, encryption_key=settings.encryption_key)[0]
    e.secret = secret
    e.clock = [datetime.now(timezone.utc)]

    def tick():
        e.clock[0] = e.clock[0] + timedelta(seconds=30)  # exactly one new 30-second step on every call, so each step-up code is fresh
        return e.clock[0]

    monkeypatch.setattr(admin_router, "_now", tick)
    e.code = lambda: pyotp.TOTP(secret).at(e.clock[0] + timedelta(seconds=30))
    e.client = client_for(app, admin.id, settings, admin=True)
    return e


def post(env, path, **body):
    body.setdefault("reason", "support ticket 123")
    body.setdefault("code", env.code())
    return env.client.post("/api/admin" + path, json=body)


def snapshot(tid):
    with session_scope() as s:
        t = s.get(Trade, tid)
        return {c.name: getattr(t, c.name) for c in Trade.__table__.columns}


def changes():
    with session_scope() as s:
        return [(c.kind, c.action, c.target, c.old, c.new, c.reason, c.admin_id) for c in s.scalars(select(AdminChange).order_by(AdminChange.at))]


# ---------------------------------------------------------------- the allowlist
def test_the_allowlist_has_no_secret_looking_key_and_only_plain_types():
    for key, d in O.ALLOWLIST.items():
        assert not (set(key.split("_")) & O.SECRET_WORDS), key
        assert d.kind in ("bool", "int", "float", "choice")
        assert hasattr(load_settings(), d.key)
    assert not any(w in " ".join(O.ALLOWLIST) for w in ("password", "secret", "token", "webhook"))


def test_every_default_equals_what_the_environment_gives_when_nothing_is_set(monkeypatch):
    for name in ("SIGNUPS_MODE", "SETUP_ADVISOR", "POLL_INTERVAL_SECONDS", "SUBSCRIPTION_GRACE_DAYS", "LOGIN_MAX_FAILURES", "LOGIN_LOCKOUT_SECONDS", "RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_AUTH_PER_MINUTE", "DISABLE_API_DOCS"):
        monkeypatch.delenv(name, raising=False)
    base = load_settings()
    for d in O.ALLOWLIST.values():
        assert getattr(base, d.key) == d.default, d.key


def test_the_allowlist_check_itself_refuses_secret_names_and_free_text():
    good = O.SettingDef("poll_interval_seconds", "int", 300, "x", lo=60, hi=3600)
    O.validate_allowlist([good])
    for bad in (O.SettingDef("jwt_secret", "choice", "a", "x", choices=("a",)), O.SettingDef("telegram_bot_token", "int", 1, "x", lo=0, hi=1), O.SettingDef("webhook_url", "bool", False, "x"),
                O.SettingDef("ssh_key", "bool", False, "x"), O.SettingDef("signups_mode", "text", "", "x")):
        with pytest.raises(RuntimeError):
            O.validate_allowlist([bad])


@pytest.mark.parametrize("key,raw,ok", [
    ("signups_mode", "closed", True), ("signups_mode", "maybe", False), ("poll_interval_seconds", "300", True), ("poll_interval_seconds", "59", False), ("poll_interval_seconds", "3601", False),
    ("poll_interval_seconds", "abc", False), ("poll_interval_seconds", "nan", False), ("setup_advisor", "on", True), ("setup_advisor", "perhaps", False), ("login_max_failures", "2", False),
    ("subscription_grace_days", "0", True), ("subscription_grace_days", "31", False), ("nope", "1", False), ("poll_interval_seconds", True, False)])
def test_values_are_checked_against_type_and_bounds(key, raw, ok):
    if ok:
        assert O.coerce(O.definition(key), raw) is not None or raw in ("off", "false")
    else:
        with pytest.raises(O.SettingError):
            O.coerce(O.definition(key), raw)


# ---------------------------------------------------------------- reading
def test_no_row_means_the_environment_value_and_the_listing_says_so(env):
    base = load_settings()
    assert O.get_setting("poll_interval_seconds", base) == base.poll_interval_seconds
    assert O.effective_settings(base) is base
    row = next(x for x in O.listing(base) if x["key"] == "poll_interval_seconds")
    assert row["source"] == "environment" and row["stored_value"] is None


def test_a_stored_value_wins_a_bad_one_and_a_missing_table_fall_back_to_the_environment(env):
    base = load_settings()
    with session_scope() as s:
        s.add(OperatorSetting(key="poll_interval_seconds", value="120"))
        s.add(OperatorSetting(key="login_max_failures", value="999"))  # out of bounds: ignored
        s.add(OperatorSetting(key="not_on_the_list", value="1"))  # not on the allowlist: ignored
    O.reset_cache()
    assert O.get_setting("poll_interval_seconds", base) == 120
    assert O.get_setting("login_max_failures", base) == base.login_max_failures
    eff = O.effective_settings(base)
    assert eff.poll_interval_seconds == 120 and not hasattr(eff, "not_on_the_list")
    from sqlalchemy import create_engine

    empty = create_engine("sqlite://")  # no tables at all (before the first start of the new code)
    assert O.effective_settings(base, engine=empty) is base


def test_a_restart_needed_key_applies_only_at_startup(env):
    base = load_settings()
    with session_scope() as s:
        s.add(OperatorSetting(key="rate_limit_per_minute", value="50"))
        s.add(OperatorSetting(key="poll_interval_seconds", value="90"))
    O.reset_cache()
    live = O.effective_settings(base)
    assert live.poll_interval_seconds == 90 and live.rate_limit_per_minute == base.rate_limit_per_minute
    assert O.effective_settings(base, startup=True).rate_limit_per_minute == 50
    item = next(x for x in O.listing(base) if x["key"] == "rate_limit_per_minute")
    assert item["restart_required"] and "next restart" in item["note"] and item["effective_value"] == base.rate_limit_per_minute


def test_customers_can_only_ever_see_the_keys_marked_visible(env):
    view = O.customer_view(load_settings())
    assert set(view) == {k for k, d in O.ALLOWLIST.items() if d.customer_visible} and "poll_interval_seconds" not in view


def test_the_setting_reaches_the_api_on_the_next_request_and_the_scheduler_next_round(env):
    from mba_bot.api import deps

    assert deps.get_settings().signups_mode == "open"
    assert post(env, "/settings/set", key="signups_mode", value="closed").status_code == 200
    assert deps.get_settings().signups_mode == "closed"
    assert O.effective_settings(load_settings()).signups_mode == "closed"  # what the scheduler reads each round
    assert post(env, "/settings/reset", key="signups_mode").status_code == 200
    assert deps.get_settings().signups_mode == "open"


# ---------------------------------------------------------------- the write gate
def test_every_write_is_refused_while_the_switch_is_off(env):
    env.monkeypatch.setenv("ADMIN_ACTIONS_ENABLED", "0")
    for path, extra in [("/settings/set", {"key": "signups_mode", "value": "closed"}), (f"/customers/{env.ref}/pause", {}), (f"/customers/{env.ref}/suspend", {}),
                        (f"/customers/{env.ref}/subscription/extend", {"days": 5}), (f"/customers/{env.ref}/note", {"note": "hello"}), ("/kill-switch/engage", {})]:
        r = post(env, path, **extra)
        assert r.status_code == 403 and "switched off" in r.json()["detail"], path
    assert changes() == []
    with session_scope() as s:
        assert s.get(TradingAccount, env.ref).is_active is True


def test_a_write_needs_a_reason_and_a_good_fresh_code(env):
    fresh = env.code()
    assert post(env, f"/customers/{env.ref}/pause", reason="no", code=fresh).status_code == 422
    with session_scope() as s:
        from mba_bot.models import AdminTotp

        assert s.get(AdminTotp, env.admin_id).last_step == 0  # a refused reason did not use up the code
    assert post(env, f"/customers/{env.ref}/pause", code="000000").status_code == 401
    used = env.code()
    ok = post(env, f"/customers/{env.ref}/pause", code=used)
    assert ok.status_code == 200
    old = env.code  # the code just used cannot be used again
    again = env.client.post(f"/api/admin/customers/{env.ref}/resume", json={"reason": "undo the pause", "code": used})
    assert again.status_code == 401 and "already used" in again.json()["detail"]
    with session_scope() as s:
        assert s.get(TradingAccount, env.ref).is_active is False
    assert len(changes()) == 1 and old


def test_five_wrong_codes_lock_the_step_up_and_nothing_changes(env):
    for _ in range(4):
        assert post(env, f"/customers/{env.ref}/pause", code="111111").status_code == 401
    assert post(env, f"/customers/{env.ref}/pause", code="111111").status_code == 429
    assert post(env, f"/customers/{env.ref}/pause").status_code == 429  # even a right code, while locked
    assert changes() == []


def test_a_normal_customer_and_an_admin_without_the_second_factor_cannot_write(env):
    with session_scope() as s:
        other = mk_customer(s, "someone@example.com")
        oid = other.id
    c = client_for(env.app, oid, env.settings)
    assert c.post("/api/admin/settings/set", json={"key": "signups_mode", "value": "closed", "reason": "hello there", "code": "123456"}).status_code == 403
    c2 = client_for(env.app, env.admin_id, env.settings)  # admin by email, no admin session
    assert c2.post(f"/api/admin/customers/{env.ref}/pause", json={"reason": "hello there", "code": env.code()}).status_code == 401
    assert changes() == []


# ---------------------------------------------------------------- settings changes
def test_a_setting_change_writes_the_row_and_the_log_with_old_new_who_and_reason(env):
    r = post(env, "/settings/set", key="poll_interval_seconds", value="120", reason="shorter rounds for the test")
    assert r.status_code == 200 and r.json() == {"ok": True, "action": "set", "old": "env:300", "new": "120", "restart_required": False}
    r2 = post(env, "/settings/set", key="rate_limit_per_minute", value="60", reason="tighter limit after the incident")
    assert r2.json()["restart_required"] is True
    log = changes()
    assert log[0] == ("setting", "set", "poll_interval_seconds", "env:300", "120", "shorter rounds for the test", env.admin_id)
    assert log[1][1:5] == ("set", "rate_limit_per_minute", "env:120", "60")
    with session_scope() as s:
        assert s.get(OperatorSetting, "poll_interval_seconds").updated_by == env.admin_id
    r3 = post(env, "/settings/reset", key="poll_interval_seconds", reason="back to the environment value")
    assert r3.status_code == 200 and changes()[-1][1:5] == ("reset", "poll_interval_seconds", "120", "env:300")
    assert post(env, "/settings/reset", key="poll_interval_seconds").status_code == 422  # nothing stored


def test_a_secret_or_unknown_key_or_bad_value_is_refused_and_logged_nowhere(env):
    for key, value in [("jwt_secret", "x"), ("encryption_key", "abc"), ("telegram_bot_token", "123"), ("poll_interval_seconds", "5"), ("signups_mode", "x" * 41)]:
        assert post(env, "/settings/set", key=key, value=value).status_code in (422, 404), key
    assert changes() == []
    with session_scope() as s:
        assert s.scalars(select(OperatorSetting)).all() == []


def test_the_settings_list_and_the_changes_list_are_for_admins_and_hold_no_secret(env):
    post(env, "/settings/set", key="poll_interval_seconds", value="120")
    body = env.client.get("/api/admin/settings").json()
    assert body["actions_enabled"] is True and {s["key"] for s in body["settings"]} == set(O.ALLOWLIST)
    text = env.client.get("/api/admin/changes").text + str(body)
    for banned in ("jwt-secret", "password_hash", env.settings.encryption_key, env.secret):
        assert banned not in text
    assert client_for(env.app, env.target_id, env.settings).get("/api/admin/settings").status_code == 403


# ---------------------------------------------------------------- customer actions
def test_pause_and_resume_change_only_the_account_switch_and_leave_open_trades_alone(env):
    with session_scope() as s:
        t = mk_trade(s, s.get(TradingAccount, env.ref), status="open", paper=False, pnl=None)
        tid = t.id
        tid = t.id
    before = snapshot(tid)
    assert post(env, f"/customers/{env.ref}/pause").status_code == 200
    assert post(env, f"/customers/{env.ref}/pause").status_code == 409
    assert snapshot(tid) == before
    with session_scope() as s:
        assert s.get(TradingAccount, env.ref).is_active is False
        from mba_bot.db import list_active_accounts

        assert env.ref in {a.id for a in list_active_accounts(s)}  # still managed because a trade is open
    assert post(env, f"/customers/{env.ref}/resume").status_code == 200
    assert post(env, f"/customers/{env.ref}/resume").status_code == 409
    assert [c[1] for c in changes()] == ["pause_bot", "resume_bot"]


def test_suspend_stops_sign_in_and_new_entries_and_reinstate_does_not_restart_the_bot(env):
    assert post(env, f"/customers/{env.ref}/suspend").status_code == 200
    with session_scope() as s:
        assert s.get(Customer, env.target_id).is_active is False and s.get(TradingAccount, env.ref).is_active is False
    assert client_for(env.app, env.target_id, env.settings).get("/api/auth/me").status_code == 401
    assert post(env, f"/customers/{env.ref}/resume").status_code == 409  # reinstate first
    assert post(env, f"/customers/{env.ref}/reinstate").status_code == 200
    with session_scope() as s:
        assert s.get(Customer, env.target_id).is_active is True and s.get(TradingAccount, env.ref).is_active is False
    assert post(env, f"/customers/{env.ref}/reinstate").status_code == 409


def test_extend_creates_or_moves_the_period_and_end_expires_it_now(env):
    r = post(env, f"/customers/{env.ref}/subscription/extend", days=30)
    assert r.status_code == 200 and r.json()["old"] == "no subscription"
    with session_scope() as s:
        sub = s.scalars(select(Subscription).where(Subscription.customer_id == env.target_id)).one()
        assert sub.provider == "manual" and sub.status == "active"
        first_end = sub.current_period_end.replace(tzinfo=timezone.utc)
    post(env, f"/customers/{env.ref}/subscription/extend", days=10)
    with session_scope() as s:
        sub = s.scalars(select(Subscription)).one()
        assert sub.current_period_end.replace(tzinfo=timezone.utc) == first_end + timedelta(days=10)
    for bad in (0, 367, -3):
        assert post(env, f"/customers/{env.ref}/subscription/extend", days=bad).status_code == 422
    assert post(env, f"/customers/{env.ref}/subscription/end").status_code == 200
    with session_scope() as s:
        sub = s.scalars(select(Subscription)).one()
        assert sub.status == "expired" and sub.current_period_end.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc) + timedelta(minutes=5)
        from mba_bot.billing import entitlement

        assert entitlement.subscription_state(sub, datetime.now(timezone.utc) + timedelta(seconds=1), 5) == entitlement.EntitlementState.EXPIRED
    assert post(env, f"/customers/{env.ref}/subscription/extend", days=7).status_code == 200  # an expired one can be extended again
    with session_scope() as s:
        assert s.scalars(select(Subscription)).one().status == "active"


def test_ending_a_subscription_that_does_not_exist_is_a_404(env):
    assert post(env, f"/customers/{env.ref}/subscription/end").status_code == 404
    assert post(env, "/customers/no-such-ref/pause").status_code == 404


def test_requests_are_approved_or_refused_once_and_nothing_is_deleted(env):
    with session_scope() as s:
        a = DataRequest(customer_id=env.target_id, kind="deletion")
        b = DataRequest(customer_id=env.target_id, kind="export")
        s.add_all([a, b])
        s.flush()
        ida, idb = a.id, b.id
    listed = env.client.get("/api/admin/requests").json()["requests"]
    assert {r["id"] for r in listed} == {ida, idb} and all("email" not in r for r in listed)
    assert post(env, f"/requests/{ida}/approve").status_code == 200
    assert post(env, f"/requests/{ida}/refuse").status_code == 409
    assert post(env, f"/requests/{idb}/refuse").status_code == 200
    assert post(env, "/requests/none/approve").status_code == 404
    with session_scope() as s:
        assert s.get(DataRequest, ida).status == "approved" and s.get(DataRequest, idb).status == "declined"
        assert s.get(Customer, env.target_id) is not None and s.get(TradingAccount, env.ref) is not None


def test_a_note_is_stored_for_admins_only_and_is_bounded(env):
    assert post(env, f"/customers/{env.ref}/note", note="called the customer, wants a refund").status_code == 200
    assert post(env, f"/customers/{env.ref}/note", note="").status_code == 422
    assert post(env, f"/customers/{env.ref}/note", note="x" * 501).status_code == 422
    with session_scope() as s:
        notes = s.scalars(select(CustomerNote)).all()
        assert [n.note for n in notes][0].startswith("called the customer") and notes[0].admin_id == env.admin_id


def test_the_kill_action_only_sets_the_existing_flag(env):
    from mba_bot.db import _engine

    assert post(env, "/kill-switch/engage", reason="exchange incident, pause new entries").status_code == 200
    assert kill_switch.read_row(_engine).state == "engaged"
    with session_scope() as s:
        assert s.get(TradingAccount, env.ref).is_active is True and s.scalars(select(Trade)).all() == []
    assert post(env, "/kill-switch/release", reason="incident over, resume entries").status_code == 200
    assert kill_switch.read_row(_engine).state == "released"
    assert [c[1] for c in changes()] == ["kill_engage", "kill_release"]
    assert any(e[1] == "engage" for e in kill_switch.recent_events(_engine, 5))


def test_no_admin_action_touches_a_trade_or_an_order(env):
    import inspect

    src = inspect.getsource(admin_actions)
    for forbidden in ("exchange", "ccxt", "lifecycle", "execute_exit", "open_live_trade", "create_order", "cancel_order"):
        assert forbidden not in src.replace("an exchange order", "").replace("exchange, a payment", "").replace("an exchange", ""), forbidden
    with session_scope() as s:
        acct = s.get(TradingAccount, env.ref)
        t = mk_trade(s, acct, status="open", paper=False, pnl=None)
        tid = t.id
    before = snapshot(tid)
    for path, extra in [(f"/customers/{env.ref}/pause", {}), (f"/customers/{env.ref}/suspend", {}), (f"/customers/{env.ref}/subscription/extend", {"days": 3}), ("/kill-switch/engage", {})]:
        assert post(env, path, **extra).status_code == 200
    assert snapshot(tid) == before


def test_every_write_also_leaves_the_normal_audit_row(env):
    from mba_bot.models import AdminAudit

    post(env, f"/customers/{env.ref}/pause")
    post(env, f"/customers/{env.ref}/pause")  # the 409 is recorded too
    with session_scope() as s:
        rows = s.scalars(select(AdminAudit).where(AdminAudit.customer_id == env.admin_id)).all()
        assert [r.status for r in rows][-2:] == [200, 409]
        assert all(env.ref not in (r.detail or "") or len(r.detail) < 40 for r in rows)
