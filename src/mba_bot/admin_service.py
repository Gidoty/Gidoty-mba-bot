"""Admin dashboard queries and masking (docs/security/admin_dashboard.md). Read-only: every function here only SELECTs.

One aggregate query per panel (GROUP BY or conditional sums), no per-customer loops, and a 60 second in-memory cache per panel and page. Mode, entitlement and result labelling use the functions the rest of the
service already uses (`onboarding.resolve_trading_mode`, `billing.entitlement.subscription_state`, `reporting.Group`): the SQL only counts rows that share the same inputs, and the real function is called once per
distinct group. Nothing here selects a password hash, a key, a token, a chat id, a payment reference or an address; the only place an email is read is the customer list and the reveal, and it leaves this module masked
(or, for a reveal, once)."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Integer, and_, case, distinct, func, literal, or_, select
from sqlalchemy.orm import Session

from . import kill_switch, reporting
from .billing import entitlement
from .models import (
    NON_TERMINAL_TRADE_STATUSES,
    PROTECTION_PROTECTED,
    TRADE_CLOSED,
    AdminAudit,
    ApiCredential,
    BillingPayment,
    Customer,
    PairSkip,
    Subscription,
    Trade,
    TradingAccount,
)
from .onboarding import TradingMode, resolve_trading_mode

PAGE_SIZE = 50
CACHE_SECONDS = 60
STALE_SCAN_MINUTES = 15
REVEAL_PER_DAY = 20
REASON_MIN, REASON_MAX = 5, 200
RESULT_BOTS = 50
WINDOWS = (7, 30, 0)  # days; 0 = all time


# ------------------------------------------------------------------------------------------------ masking
def mask_email(email: str | None) -> str:
    """First character, three stars, then the domain: 'a***@gmail.com'. Plus-addressing, unicode, a one-character local part and a value with no '@' are all handled; the part after the first character is never shown."""
    if not isinstance(email, str) or not email:
        return "***"
    local, at, domain = email.strip().partition("@")
    if not at:
        return "***"
    first = local[:1]
    return f"{first}***@{domain}"


def reason_problem(reason) -> str | None:
    if not isinstance(reason, str) or not (REASON_MIN <= len(reason.strip()) <= REASON_MAX):
        return f"Give a reason of {REASON_MIN} to {REASON_MAX} characters."
    return None


# ------------------------------------------------------------------------------------------------ 60 second cache
_cache: dict = {}


def reset_cache() -> None:
    _cache.clear()


def cached(key, fn, *, clock=time.monotonic, ttl: float = CACHE_SECONDS):
    now = clock()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ entitlement ("paid")
# Who may open live trades is decided by `entitlement.is_entitled` (flag off: exactly `has_paid`; flag on: the subscription rules). This module only reads the `has_paid` flag and the subscription columns as INPUTS
# to that function (tests/test_entitlement.py allows exactly this file for that reason): rows that share the same inputs are counted in SQL and the real function is called once per distinct group.
def _sub_columns(now: datetime, grace_days: int) -> list:
    return [
        Subscription.status.label("sub_status"),
        Subscription.current_period_end.is_(None).label("no_end"),
        (Subscription.current_period_end > now).label("in_period"),
        Subscription.grace_until.is_not(None).label("has_grace"),
        (Subscription.grace_until > now).label("grace_open"),
        (Subscription.current_period_end > now - timedelta(days=grace_days)).label("in_default_grace"),
    ]


def _stub_subscription(status, no_end, in_period, has_grace, grace_open, in_default, now: datetime, grace_days: int):
    """A subscription with times chosen to reproduce the SQL answers (before the period end, inside the default grace, past it), so the real rule gives the same state as it would for the row."""
    if status is None:
        return None
    end = None if no_end else (now + timedelta(hours=1) if in_period else (now - timedelta(hours=1) if in_default else now - timedelta(days=grace_days + 1)))
    grace = None if not has_grace else (now + timedelta(hours=1) if grace_open else now - timedelta(hours=2))
    return SimpleNamespace(status=status, current_period_end=end, grace_until=grace)


def _entitled(has_paid: bool, sub_inputs, now: datetime, settings) -> bool:
    customer = SimpleNamespace(has_paid=bool(has_paid), subscription=_stub_subscription(*sub_inputs, now, settings.subscription_grace_days))
    return entitlement.is_entitled(customer, settings=settings, now=now)


# ------------------------------------------------------------------------------------------------ customers
def customers_panel(db: Session, now: datetime, settings) -> dict:
    """Total, active, paid (entitled: exactly `has_paid` while billing is off), new in 7 and 30 days: one grouped query, the real entitlement rule once per distinct group."""
    grace_days = settings.subscription_grace_days
    new7, new30 = (Customer.created_at >= now - timedelta(days=7)).label("new7"), (Customer.created_at >= now - timedelta(days=30)).label("new30")
    cols = [Customer.is_active, Customer.has_paid, *_sub_columns(now, grace_days), new7, new30]
    rows = db.execute(select(*cols, func.count()).select_from(Customer).outerjoin(Subscription, Subscription.customer_id == Customer.id).group_by(*cols)).all()
    out = {"total": 0, "active": 0, "paid": 0, "new_7_days": 0, "new_30_days": 0}
    for r in rows:
        n = r[-1]
        out["total"] += n
        out["active"] += n if r[0] else 0
        out["paid"] += n if _entitled(r[1], r[2:8], now, settings) else 0
        out["new_7_days"] += n if r[8] else 0
        out["new_30_days"] += n if r[9] else 0
    return out


# ------------------------------------------------------------------------------------------------ bots by mode
MODE_PRACTICE, MODE_LIVE, MODE_LIVE_STOPPED, MODE_INACTIVE = "practice", "live", "live_stopped", "inactive"


def classify(*, is_active: bool, has_paid: bool, trial_over: bool, bypass: bool, live_enabled: bool, mainnet: bool, started: bool) -> str:
    """One account's mode, using the same `resolve_trading_mode` the scheduler uses. `live_stopped` = paid, live trading enabled and a mainnet key on file but the bot not started (it would be live once started)."""
    if not is_active:
        return MODE_INACTIVE
    state = resolve_trading_mode(
        trial_ends_at=datetime(2000, 1, 1, tzinfo=timezone.utc) if trial_over else datetime(2999, 1, 1, tzinfo=timezone.utc),
        live_bypass_accepted_at=datetime(2000, 1, 1, tzinfo=timezone.utc) if bypass else None,
        has_paid=has_paid,
        live_trading_enabled_at=datetime(2000, 1, 1, tzinfo=timezone.utc) if live_enabled else None,
        has_mainnet_credential=mainnet,
        bot_started_at=datetime(2000, 1, 1, tzinfo=timezone.utc) if started else None,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    if state.mode == TradingMode.LIVE:
        return MODE_LIVE
    if has_paid and live_enabled and mainnet and not started:
        return MODE_LIVE_STOPPED
    return MODE_PRACTICE


def bots_panel(db: Session, now: datetime, settings) -> dict:
    """Counts of bots by mode: ONE grouped query over the inputs of the mode rule (including the entitlement inputs), then the real rules once per distinct combination."""
    has_cred = case((ApiCredential.environment == "mainnet", True), else_=False)
    cols = [
        TradingAccount.is_active, Customer.has_paid, *_sub_columns(now, settings.subscription_grace_days), (TradingAccount.trial_ends_at <= now).label("trial_over"),
        TradingAccount.live_bypass_accepted_at.is_not(None).label("bypass"), TradingAccount.live_trading_enabled_at.is_not(None).label("live_enabled"), has_cred.label("mainnet"),
        TradingAccount.bot_started_at.is_not(None).label("started"),
    ]
    rows = db.execute(
        select(*cols, func.count()).select_from(TradingAccount).join(Customer, Customer.id == TradingAccount.customer_id).outerjoin(Subscription, Subscription.customer_id == Customer.id)
        .outerjoin(ApiCredential, ApiCredential.account_id == TradingAccount.id).group_by(*cols)
    ).all()
    out = {MODE_PRACTICE: 0, MODE_LIVE: 0, MODE_LIVE_STOPPED: 0, MODE_INACTIVE: 0}
    for r in rows:
        entitled = _entitled(r[1], r[2:8], now, settings)
        out[classify(is_active=bool(r[0]), has_paid=entitled, trial_over=bool(r[8]), bypass=bool(r[9]), live_enabled=bool(r[10]), mainnet=bool(r[11]), started=bool(r[12]))] += r[13]
    out["total"] = sum(out.values())
    return out


# ------------------------------------------------------------------------------------------------ health
def deploy_commit(root: Path | None = None) -> str | None:
    """The short commit the service runs: DEPLOY_COMMIT from the environment, else read from the checkout's .git (two small file reads, no command). None when neither is there."""
    env = (os.environ.get("DEPLOY_COMMIT") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", env):
        return env[:7].lower()
    root = root or Path(__file__).resolve().parents[2]
    try:
        head = (root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            head = (root / ".git" / head.split(" ", 1)[1].strip()).read_text().strip()
        return head[:7].lower() if re.fullmatch(r"[0-9a-fA-F]{7,40}", head) else None
    except OSError:
        return None


def health_panel(db: Session, now: datetime, *, commit: str | None = None) -> dict:
    stale_before = now - timedelta(minutes=STALE_SCAN_MINUTES)
    a = db.execute(
        select(
            func.min(case((TradingAccount.is_active.is_(True), TradingAccount.last_scanned_at))),
            func.coalesce(func.sum(case((and_(TradingAccount.is_active.is_(True), TradingAccount.last_error.is_not(None)), 1), else_=0)), 0),
            func.coalesce(func.sum(case((and_(TradingAccount.is_active.is_(True), or_(TradingAccount.last_scanned_at.is_(None), TradingAccount.last_scanned_at < stale_before)), 1), else_=0)), 0),
            func.count(TradingAccount.id),
        )
    ).one()
    t = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((and_(Trade.is_paper.is_(False), or_(Trade.protection_status.is_(None), Trade.protection_status != PROTECTION_PROTECTED)), 1), else_=0)), 0),
        ).where(Trade.status.in_(NON_TERMINAL_TRADE_STATUSES))
    ).one()
    ks = kill_switch.read_row(db.get_bind())
    oldest = _aware(a[0])
    return {
        "bots": a[3],
        "oldest_last_scan_age_seconds": None if oldest is None else max(0, int((now - oldest).total_seconds())),
        "bots_with_error_now": int(a[1]),
        "bots_not_scanned_15_min": int(a[2]),
        "open_trades": t[0],
        "open_trades_protection_not_healthy": int(t[1]),
        "kill_switch": ks.state,
        "last_deploy_commit": commit,
    }


# ------------------------------------------------------------------------------------------------ results
def _label(g: reporting.Group) -> str:
    if g.mode == reporting.MODE_PAPER:
        return "Simulated, no fees or slippage"
    if g.fees_complete:
        return "Net after fees (every fee known)"
    known = f", fees known for {g.trades_with_fees} of {g.closed}" if g.trades_with_fees else ""
    return f"Before fees{known}"


def _group(mode: str, strategy: str, r) -> reporting.Group:
    g = reporting.Group(mode, strategy)
    g.closed, g.wins, g.losses = int(r.closed), int(r.wins), int(r.losses)
    g.gross_pnl = float(r.gross or 0.0)
    g.fees, g.trades_with_fees = float(r.fees or 0.0), int(r.with_fees)
    return g


def _group_dict(g: reporting.Group) -> dict:
    d = g.to_dict()
    d["label"] = _label(g)
    d["mode_label"] = reporting.MODE_LABEL[g.mode]
    return {k: d[k] for k in ("mode", "strategy", "closed_trades", "wins", "losses", "gross_pnl", "fees", "trades_with_fees", "fees_complete", "net_pnl", "simulated", "label", "mode_label")}


def _result_columns():
    mode = case((Trade.is_demo.is_(True), reporting.MODE_DEMO), (Trade.is_paper.is_(True), reporting.MODE_PAPER), else_=reporting.MODE_LIVE).label("mode")
    strategy = func.coalesce(Trade.strategy_id, reporting.EMA_ID).label("strategy")
    has_fees = and_(mode != reporting.MODE_PAPER, Trade.entry_fee_quote.is_not(None), Trade.exit_fee_quote.is_not(None))
    aggs = (
        func.count().label("closed"),
        func.coalesce(func.sum(case((Trade.pnl > 0, 1), else_=0)), 0).label("wins"),
        func.coalesce(func.sum(case((Trade.pnl < 0, 1), else_=0)), 0).label("losses"),
        func.coalesce(func.sum(Trade.pnl), 0.0).label("gross"),
        func.coalesce(func.sum(case((has_fees, Trade.entry_fee_quote + Trade.exit_fee_quote), else_=0.0)), 0.0).label("fees"),
        func.coalesce(func.sum(case((has_fees, 1), else_=0)), 0).label("with_fees"),
    )
    return mode, strategy, aggs


def results_panel(db: Session, now: datetime, *, window_days: int = 30) -> dict:
    """Closed-trade results, live and practice (and demo) never added together, by strategy, gross and net exactly as `reporting.Group` defines them. Two queries: totals per mode and strategy over all
    bots, and the same per bot for the 50 bots with the most closed trades in the window."""
    mode, strategy, aggs = _result_columns()
    where = [Trade.status == TRADE_CLOSED, Trade.pnl.is_not(None)]
    if window_days:
        where.append(Trade.closed_at >= now - timedelta(days=window_days))
    totals = db.execute(select(mode, strategy, *aggs).where(*where).group_by(mode, strategy)).all()
    top = select(Trade.account_id).where(*where).group_by(Trade.account_id).order_by(func.count().desc(), Trade.account_id).limit(RESULT_BOTS)
    per_bot = db.execute(
        select(TradingAccount.bot_code, TradingAccount.pair, TradingAccount.market_type, mode, strategy, *aggs)
        .join(TradingAccount, TradingAccount.id == Trade.account_id)
        .where(*where, Trade.account_id.in_(top))
        .group_by(Trade.account_id, TradingAccount.bot_code, TradingAccount.pair, TradingAccount.market_type, mode, strategy)
        .order_by(TradingAccount.bot_code)
    ).all()
    bots: dict = {}
    for r in per_bot:
        b = bots.setdefault(r.bot_code or "unknown", {"bot_code": r.bot_code or "unknown", "pair": r.pair, "market_type": r.market_type, "groups": []})
        b["groups"].append(_group_dict(_group(r.mode, r.strategy, r)))
    order = {reporting.MODE_LIVE: 0, reporting.MODE_DEMO: 1, reporting.MODE_PAPER: 2}
    for b in bots.values():
        b["groups"].sort(key=lambda g: (order[g["mode"]], g["strategy"]))
    return {
        "window_days": window_days or None,
        "funding_note": reporting.FUNDING_NOTE,
        "totals": sorted((_group_dict(_group(r.mode, r.strategy, r)) for r in totals), key=lambda g: (order[g["mode"]], g["strategy"])),
        "bots": list(bots.values()),
    }


# ------------------------------------------------------------------------------------------------ customer list and reveal
def customer_list(db: Session, now: datetime, settings, *, page: int) -> dict:
    page = max(1, int(page))
    total = db.execute(select(func.count(Customer.id))).scalar_one()
    has_cred = case((ApiCredential.environment == "mainnet", True), else_=False)
    rows = db.execute(
        select(
            Customer.email, Customer.is_active, Customer.has_paid, Customer.created_at, TradingAccount.id, TradingAccount.pair, TradingAccount.market_type, TradingAccount.is_active,
            TradingAccount.trial_ends_at, TradingAccount.live_bypass_accepted_at, TradingAccount.live_trading_enabled_at, TradingAccount.bot_started_at, TradingAccount.last_scanned_at,
            TradingAccount.last_error.is_not(None), TradingAccount.last_error_at, has_cred, *_sub_columns(now, settings.subscription_grace_days),
        )
        .select_from(Customer).outerjoin(TradingAccount, TradingAccount.customer_id == Customer.id).outerjoin(ApiCredential, ApiCredential.account_id == TradingAccount.id)
        .outerjoin(Subscription, Subscription.customer_id == Customer.id)
        .order_by(Customer.created_at.desc(), Customer.id).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
    ).all()
    items = []
    for r in rows:
        scanned = _aware(r[12])
        mode = None
        entitled = _entitled(r[2], r[16:22], now, settings)
        if r[4] is not None:
            mode = classify(
                is_active=bool(r[7]), has_paid=entitled, trial_over=_aware(r[8]) <= now, bypass=r[9] is not None, live_enabled=r[10] is not None, mainnet=bool(r[15]), started=r[11] is not None
            )
        items.append({
            "ref": r[4], "masked_email": mask_email(r[0]), "account_prefix": (r[4] or "")[:8] or None, "pair": r[5], "market_type": r[6], "mode": mode,
            "customer_active": bool(r[1]), "paid": entitled, "created_at": _aware(r[3]),
            "last_scan_age_seconds": None if scanned is None else max(0, int((now - scanned).total_seconds())),
            "has_error": bool(r[13]), "last_error_at": _aware(r[14]),
        })
    return {"page": page, "page_size": PAGE_SIZE, "total": total, "items": items}


def reveals_today(db: Session, admin_id: str, now: datetime) -> int:
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.execute(select(func.count(AdminAudit.id)).where(AdminAudit.customer_id == admin_id, AdminAudit.action == "reveal", AdminAudit.status == 200, AdminAudit.at >= start)).scalar_one()


def email_of(db: Session, ref: str) -> str | None:
    """The full email of the customer who owns account `ref`. Only the reveal route calls this."""
    return db.execute(select(Customer.email).join(TradingAccount, TradingAccount.customer_id == Customer.id).where(TradingAccount.id == ref)).scalar_one_or_none()


# ------------------------------------------------------------------------------------------------ skipped pairs
def skips_panel(db: Session, now: datetime) -> dict:
    since = (now - timedelta(days=7)).date().isoformat()
    rows = db.execute(
        select(PairSkip.reason, func.coalesce(func.sum(PairSkip.count), 0), func.count(distinct(PairSkip.account_id))).where(PairSkip.day_key >= since).group_by(PairSkip.reason).order_by(PairSkip.reason)
    ).all()
    return {"days": 7, "reasons": [{"reason": r[0], "skips": int(r[1]), "accounts": r[2]} for r in rows]}


# ------------------------------------------------------------------------------------------------ billing (read only)
def billing_panel(db: Session, now: datetime, settings) -> dict:
    grace_days = settings.subscription_grace_days
    nul = Subscription.current_period_end.is_(None)
    soon = func.coalesce(func.sum(case((and_(Subscription.status.in_((entitlement.ACTIVE_STATUS, entitlement.CANCELLED_STATUS)), Subscription.current_period_end > now, Subscription.current_period_end <= now + timedelta(days=7)), 1), else_=0)), 0)
    cols = _sub_columns(now, grace_days)
    subs = db.execute(select(*cols, func.count(), soon).select_from(Subscription).group_by(*cols)).all()
    states = {"active": 0, "grace": 0, "expired": 0, "cancelled": 0}
    expiring = 0
    for r in subs:
        expiring += int(r[7])
        state = entitlement.subscription_state(_stub_subscription(*r[:6], now, grace_days), now, grace_days).value
        states[state if state in states else "expired"] += r[6]
    since = now - timedelta(days=30)
    pays = db.execute(
        select(BillingPayment.provider, BillingPayment.status, BillingPayment.currency, func.count(), func.coalesce(func.sum(BillingPayment.amount_minor), 0))
        .where(BillingPayment.issued_at >= since).group_by(BillingPayment.provider, BillingPayment.status, BillingPayment.currency).order_by(BillingPayment.provider, BillingPayment.status)
    ).all()
    payments = [{"provider": p, "status": s_, "currency": cur, "count": n, "amount_minor": int(amt)} for p, s_, cur, n, amt in pays]
    return {
        "subscriptions": states,
        "expiring_in_7_days": expiring,
        "payments_30_days": payments,
        "failed_or_mismatched_30_days": sum(p["count"] for p in payments if p["status"] in ("failed", "underpaid")),
        "switches": {
            "subscription_billing_enabled": bool(settings.subscription_billing_enabled), "paystack_verify_mode": str(settings.paystack_verify_mode), "nowpayments_enabled": bool(settings.nowpayments_enabled),
            "nowpayments_ipn_mode": str(settings.nowpayments_ipn_mode),
        },
    }
