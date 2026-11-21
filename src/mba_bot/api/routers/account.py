"""The customer's trading account: onboarding, settings, pause/resume,
and the live-trading bypass - all reusing the same `onboarding.py` and
`crypto_utils.py` functions the CLI signup script uses, not a parallel
implementation of the trial/bypass rules.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ... import exchange, pair_catalog, pnl_labels
from ... import leverage as leverage_module
from ...billing.entitlement import is_entitled
from ...botcode import assign_unique_bot_code
from ...config import Settings, manual_pairs
from ...crypto_utils import decrypt, encrypt
from ...db import get_open_trade, list_open_trades
from ...multi_trade import strategy_of
from ...models import PROTECTION_PROTECTED, TRADE_ATTENTION
from ...legal import RISK_DISCLOSURE_VERSION, has_current_acceptance, record_acceptance
from ... import setup_advice, account_loss, daily_limit, demo as demo_module, live_multi, risk_limits, small_capital
from ...models import ApiCredential, Customer, DemoCredential, TradingAccount
from ...onboarding import (
    LIVE_TRADING_ACTIVATION_DISCLOSURE,
    LIVE_TRADING_DISCLOSURE,
    accept_live_bypass,
    enable_live_trading,
    is_live_eligible,
    resolve_trading_mode,
    start_bot,
    start_trial,
)
from ...risk import InvalidRiskParameters, clamp_risk_pct
from ..deps import get_db, get_pair_facts, get_settings, require_legal_acceptance
from ..schemas import (
    AccountCreateRequest,
    AccountOut,
    AccountUpdateRequest,
    BalanceOut,
    CredentialTestOut,
    CredentialUpdateRequest,
    DemoCredentialsRequest,
    DemoModeRequest,
    EnableLiveTradingRequest,
    LiveBypassRequest,
    OpenTradesOut,
    OpenTradeOut,
    PositionOut,
    StartBotRequest,
    TotalLossResetRequest,
    BinanceSettingsOut,
    AdviceOut,
    AdviceRequest,
)

router = APIRouter(prefix="/api/account", tags=["account"])


DAILY_LIMIT_REQUIRED_TEXT = "set your daily loss limit on the Risk tab of Bot Settings before turning on live trading"


def _require_account(customer: Customer) -> TradingAccount:
    if customer.account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet - POST /api/account first")
    return customer.account


def _paid(customer: Customer, settings: Settings) -> bool:
    """The one question every handler below asks: may this customer open NEW live trades? Exactly `customer.has_paid`
    unless SUBSCRIPTION_BILLING_ENABLED is on (billing/entitlement.py)."""
    return is_entitled(customer, settings=settings)


def _check_risk_setting(db: Session, customer: Customer, risk_pct: float, *, ack_high: bool, ack_very_high: bool, settings: Settings) -> list[str]:
    """Server-side rule for a customer's risk per trade (docs/safety/risk_per_trade.md). Raises 422 with a plain message if the number is outside 0.1 up to the current RISK_PCT_MAX
    (it is never silently lowered), or if it needs an acknowledgment that is neither on file for the current version nor ticked in this request. Returns the acknowledgment
    types to record (ticked now, not on file yet); the caller records them only once the whole request is going through."""
    on_file = {t: has_current_acceptance(db, customer_id=customer.id, document_type=t, version=risk_limits.ACK_VERSION) for t in (risk_limits.ACK_HIGH, risk_limits.ACK_VERY_HIGH)}
    problem, to_record = risk_limits.risk_setting_problem(risk_pct, settings.risk_pct_max, on_file=on_file, ticked={risk_limits.ACK_HIGH: ack_high, risk_limits.ACK_VERY_HIGH: ack_very_high})
    if problem is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    return to_record


def _day_keys(settings: Settings | None, now: datetime | None = None) -> tuple[str, str]:
    """(today, tomorrow) as local day keys in the account day time zone (Lagos by default; an invalid name falls back to Lagos here: the trader itself fails closed on it)."""
    now = now or datetime.now(timezone.utc)
    tz = account_loss.zone(settings.account_day_timezone if settings else "Africa/Lagos") or account_loss.zone("Africa/Lagos")
    today = account_loss.local_date(now, tz)
    return today.isoformat(), (today + timedelta(days=1)).isoformat()


def _daily_limit_fields(account: TradingAccount, *, db: Session, settings: Settings | None) -> dict:
    """The daily loss limit as the screen needs it. Reads only; a pending raise whose day has arrived is shown as the amount in force (the trader promotes it on its first check of the day)."""
    today, _ = _day_keys(settings)
    r = daily_limit.resolve(account.daily_loss_limit_usd, account.daily_loss_limit_pending_usd, account.daily_loss_limit_pending_from, today)
    ref = daily_limit.reference_value(account, db)
    return dict(
        daily_loss_limit_usd=r.amount,
        daily_loss_limit_pending_usd=r.pending_amount,
        daily_loss_limit_pending_from=r.pending_from,
        daily_loss_limit_is_default=bool(account.daily_loss_limit_is_default),
        daily_loss_limit_required=r.amount is None,
        daily_loss_limit_enforced=bool(settings.account_daily_loss_limit) if settings else False,
        daily_loss_limit_pct=daily_limit.pct_of(r.amount, ref),
        daily_loss_limit_suggested_usd=daily_limit.suggested_usd(ref),
        daily_loss_limit_max_usd=daily_limit.total_stop_level_usd(ref),
        account_value_reference=ref,
        one_trade_can_exceed_limit=daily_limit.one_trade_can_exceed(account.risk_pct, account.allocated_capital, r.amount),
    )


def _pair_skip_fields(account: TradingAccount, *, db: Session, settings: Settings | None) -> dict:
    """SMALL_CAPITAL_PAIRS off (default): nothing. On: the plain message for the account's own pair when the bot skipped it in the last 24 hours for being too small for the capital."""
    if settings is None or not settings.small_capital_pairs:
        return {}
    row = small_capital.latest_for_pair(db, account.id, account.pair)
    if row is None:
        return {}
    return dict(pair_skip_message=small_capital.row_message(row), pair_skip_min_capital=row.min_capital)


def _to_account_out(account: TradingAccount, *, has_paid: bool, db: Session, settings: Settings | None = None) -> AccountOut:
    state = resolve_trading_mode(
        trial_ends_at=account.trial_ends_at,
        live_bypass_accepted_at=account.live_bypass_accepted_at,
        has_paid=has_paid,
        live_trading_enabled_at=account.live_trading_enabled_at,
        has_mainnet_credential=account.has_mainnet_credential,
        bot_started_at=account.bot_started_at,
        demo_enabled=bool(settings is not None and demo_module.available(account, settings)),
    )

    # Priority order: `is_active` has no customer-facing control anymore
    # (Pause was removed - see api/routers/account.py's update_account),
    # but the column and this check stay as the internal/admin-only
    # kill switch the scheduler has always honored (db.list_active_accounts,
    # scheduler.py's _run_one) - "paused" here would only ever be seen if
    # something sets it directly, not through any customer action.
    # "not_started" is specific to the live track: every other gate met
    # (paid, mainnet credential, live trading enabled) except the
    # customer hasn't pressed Start Bot yet - doesn't apply to Testnet,
    # where scanning has always been automatic and unlimited. An error
    # is the next most actionable thing to surface, even if a trade
    # happens to be open; otherwise an open trade is worth calling out
    # over the default "scanning" state. "error" is never sticky past
    # the problem - trader.py clears last_error on the next healthy cycle.
    if not account.is_active:
        runtime_status = "paused"
    elif has_paid and account.has_mainnet_credential and account.live_trading_enabled_at is not None and account.bot_started_at is None:
        runtime_status = "not_started"
    elif (open_trades := list_open_trades(db, account.id)) and any(t.status == TRADE_ATTENTION for t in open_trades):
        # A live position the bot cannot currently vouch for (unprotected,
        # unresolved entry, residual exposure, ...). Takes precedence over
        # "error": it is the more specific, more urgent condition.
        runtime_status = "attention"
    elif account.withdrawal_blocked_at is not None and not open_trades:
        # No position, and the stored key can withdraw: trading is blocked
        # until the credential is replaced.
        runtime_status = "error"
    elif account.last_error is not None:
        runtime_status = "error"
    elif open_trades:
        runtime_status = "trade_open"
    else:
        runtime_status = "active"

    return AccountOut(
        id=account.id,
        bot_code=account.bot_code,
        pnl_basis=pnl_labels.basis(),
        pair=account.pair,
        market_type=account.market_type,
        notification_channel=account.notification_channel,
        notification_target=account.notification_target,
        risk_pct=account.risk_pct,
        mode=state.mode.value,
        trial_ends_at=account.trial_ends_at,
        paper_balance=account.paper_balance,
        live_bypass_accepted_at=account.live_bypass_accepted_at,
        is_active=account.is_active,
        credential_environment=account.credential.environment if account.credential else None,
        has_mainnet_credential=account.has_mainnet_credential,
        live_trading_enabled_at=account.live_trading_enabled_at,
        bot_started_at=account.bot_started_at,
        allocated_capital=account.allocated_capital,
        pair_selection_mode=account.pair_selection_mode,
        max_leverage=leverage_module.account_cap(account.max_leverage, settings.futures_max_leverage if settings else None),
        max_leverage_limit=leverage_module.limit(settings.futures_max_leverage if settings else None),
        risk_pct_limit=settings.risk_pct_max if settings else 2.0,
        total_loss_stop_enabled=bool(settings.account_total_loss_stop) if settings else False,
        total_loss_stop_active=account_loss.active_total_stop(db, account.id) is not None,
        total_loss_stop_threshold_pct=risk_limits.total_loss_threshold(),
        total_loss_reference_equity=account.loss_reference_equity,
        **_daily_limit_fields(account, db=db, settings=settings),
        **_pair_skip_fields(account, db=db, settings=settings),
        reports_available=bool(settings.reporting_v2) if settings else False,
        live_slots=live_multi.slot_cap(account.live_slots, switch_on=True) if (settings and settings.live_multi_position) else 1,
        live_multi_available=bool(settings.live_multi_position) if settings else False,
        live_pair_count=len(live_multi.live_pairs(account)),
        live_pair_list=[p for p in (account.pair_list or "").split(",") if p.strip()] if (settings and settings.live_multi_position) else None,
        live_open_risk_cap_pct=settings.live_total_risk_cap_pct if (settings and settings.live_multi_position) else None,
        demo_available=bool(settings.demo_runner) if settings else False,
        demo_has_keys=account.demo_credential is not None,
        demo_enabled_at=account.demo_enabled_at,
        runtime_status=runtime_status,
        last_scanned_at=account.last_scanned_at,
        last_signal=account.last_signal,
        last_signal_at=account.last_signal_at,
        last_error=account.last_error
        or (exchange.WITHDRAWAL_BLOCK_MESSAGE if account.withdrawal_blocked_at is not None else None),
        last_error_at=account.last_error_at,
        credential_blocked=account.withdrawal_blocked_at is not None,
        credential_block_message=exchange.WITHDRAWAL_BLOCK_MESSAGE if account.withdrawal_blocked_at is not None else None,
    )


def _pair_list_problem(body: AccountUpdateRequest, account: TradingAccount, db: Session, *, settings: Settings, facts: dict | None) -> tuple[int, str] | None:
    """The rules for the customer's extra live pairs, checked before anything is changed: (http status, plain text) or None. The pair-by-pair availability is the same check a single pair uses
    (`_pair_selection_problem`), plus the small-capital check when that switch is on."""
    if not settings.live_multi_position:
        return 409, "choosing extra pairs is not available on this service"
    pairs = body.pair_list or []
    mode = body.pair_selection_mode or account.pair_selection_mode
    slots = live_multi.slot_cap(body.live_slots if body.live_slots is not None else account.live_slots, switch_on=True)
    market = body.market_type or account.market_type
    primary = body.pair or account.pair
    capital = body.allocated_capital if body.allocated_capital is not None else account.allocated_capital
    if pairs and mode == "auto":
        return 422, "Auto Select trades one pair at a time. Switch pair selection to Manual to choose extra pairs."
    if pairs and slots < 2:
        return 422, "Set the number of open trades to 2 or 3 before choosing extra pairs."
    if len(pairs) > slots - 1:
        return 422, f"With {slots} open trades you can choose at most {slots - 1} extra pair(s)."
    if primary in pairs:
        return 422, f"{primary} is already your main pair. Choose a different pair."
    for p in pairs:
        problem = _pair_selection_problem(p, market, allocated_capital=capital, settings=settings, facts=facts)
        if problem is not None:
            return 422, problem
        if settings.small_capital_pairs and market == "futures" and facts is not None and facts.get(p) is not None:
            cap = leverage_module.account_cap(body.max_leverage if body.max_leverage is not None else account.max_leverage, settings.futures_max_leverage)
            v = small_capital.assess(type("R", (), {"min_notional": facts[p].min_notional, "market_min_qty": 0, "step": 0})(), None, capital=capital, leverage_cap=small_capital.leverage_cap_for(market, cap))
            if v.viable is False:
                return 422, small_capital.message(p, v)
    kept = {primary, *pairs}
    for t in list_open_trades(db, account.id):
        if t.pair not in kept:
            return 409, f"You have an open trade on {t.pair}. Keep {t.pair} in your pair list until it is closed."
    return None


def _require_no_open_trade(db: Session, account: TradingAccount, *, action: str) -> None:
    """Shared guard for anything that could orphan a position still being
    managed: changing pair, market type, the stored credential, or the
    live-trading activation switch. trader.py always acts on the
    account's *current* configuration, never what a trade was opened
    under, so changing any of these mid-trade would have the next cycle
    try to manage that position through the wrong client entirely."""
    if get_open_trade(db, account.id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"you have an open trade on {account.pair} ({account.market_type}) - wait for it to close, or close it "
            f"yourself on Binance, before {action}",
        )


def _pair_selection_problem(pair: str, market_type: str, *, allocated_capital: float | None, settings: Settings, facts: dict | None) -> str | None:
    """A customer-safe reason this pair cannot be chosen for this market, or
    None. Membership first (extra Futures pairs exist only for Futures with the
    expansion on), then - for Futures with the expansion on and exchange data
    available - listing and capital (pair_catalog). Called only when a
    customer CHANGES pair or market, never for an unchanged selection."""
    if pair not in manual_pairs(market_type, futures_expansion=settings.futures_pair_expansion):
        return f"{pair} is not available for {market_type.capitalize()}."
    if market_type == "futures" and settings.futures_pair_expansion and facts is not None:
        status_ = pair_catalog.assess_pair(
            pair, facts.get(pair), allocated_capital=allocated_capital, max_notional_multiplier=settings.max_notional_multiplier
        )
        if not status_.available:
            return status_.reason
    return None


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    body: AccountCreateRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    if customer.account is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this customer already has a trading account")
    problem = _pair_selection_problem(body.pair, body.market_type, allocated_capital=None, settings=settings, facts=None)
    if problem is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    if body.notification_channel == "whatsapp":
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "WhatsApp notifications aren't available yet - use telegram")
    if not settings.encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ENCRYPTION_KEY is not configured")
    if (body.api_key is None) != (body.api_secret is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "api_key and api_secret must be provided together")
    # Mainnet credential entry only ever becomes available after payment
    # - checked server-side, not just left to the frontend never
    # offering the option. An unpaid customer could never reach LIVE
    # anyway (resolve_trading_mode's own has_paid gate), but we also
    # don't want a real Binance key sitting in storage for an account
    # that was never meant to have one yet.
    if body.environment == "mainnet" and not _paid(customer, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "pay to unlock before connecting a mainnet Binance account")

    requested = body.risk_pct if body.risk_pct is not None else settings.risk_pct_default
    to_record = _check_risk_setting(db, customer, requested, ack_high=body.acknowledge_high_risk, ack_very_high=body.acknowledge_very_high_risk, settings=settings)
    try:
        risk_pct = clamp_risk_pct(requested, max_risk_pct=settings.risk_pct_max)  # the last guard; the check above has already refused anything above the limit
    except InvalidRiskParameters as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    for doc_type in to_record:
        record_acceptance(db, customer_id=customer.id, document_type=doc_type, version=risk_limits.ACK_VERSION)

    try:
        bot_code = assign_unique_bot_code(db)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    account = TradingAccount(
        customer_id=customer.id,
        bot_code=bot_code,
        pair=body.pair,
        market_type=body.market_type,
        notification_channel=body.notification_channel,
        risk_pct=risk_pct,
        trial_ends_at=start_trial(trial_days=settings.trial_days),
        paper_balance=settings.paper_starting_balance,
    )
    db.add(account)
    db.flush()

    # No credential at all is a valid starting state - paper-mode
    # trading never reads it (see trader.py), so a fresh unpaid account
    # has nothing to store yet. One gets created later by the Connect
    # Binance step (PUT /api/account/credentials), the same codepath
    # that handles `account.credential is None` already.
    if body.api_key is not None and body.api_secret is not None:
        db.add(
            ApiCredential(
                account_id=account.id,
                exchange="binance",
                environment=body.environment,
                encrypted_api_key=encrypt(body.api_key, key=settings.encryption_key),
                encrypted_api_secret=encrypt(body.api_secret, key=settings.encryption_key),
            )
        )
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


@router.get("", response_model=AccountOut)
def get_account(customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> AccountOut:
    return _to_account_out(_require_account(customer), has_paid=_paid(customer, settings), db=db, settings=settings)


@router.patch("", response_model=AccountOut)
def update_account(
    body: AccountUpdateRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    facts: dict | None = Depends(get_pair_facts),
) -> AccountOut:
    account = _require_account(customer)
    ack_to_record: list[str] = []
    if body.risk_pct is not None:
        # Checked before anything is changed: a number above the limit is refused (never silently lowered), and a number above 2% or from 10% needs its recorded acknowledgment.
        ack_to_record = _check_risk_setting(db, customer, body.risk_pct, ack_high=body.acknowledge_high_risk, ack_very_high=body.acknowledge_very_high_risk, settings=settings)

    if body.daily_loss_limit_usd is not None:
        # Checked before anything is changed, like the risk setting above: the amount must be above 0 and below the dollar level of the automatic total stop.
        problem = daily_limit.validate_amount(body.daily_loss_limit_usd, reference_value=daily_limit.reference_value(account, db))
        if problem is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)

    refusal = live_multi.slots_refusal(
        new_slots=body.live_slots, new_selection_mode=body.pair_selection_mode, current_slots=account.live_slots,
        current_selection_mode=account.pair_selection_mode, switch_on=settings.live_multi_position,
    )
    if refusal is not None:
        raise HTTPException(refusal[0], refusal[1])

    if body.pair_list is not None:
        refusal = _pair_list_problem(body, account, db, settings=settings, facts=facts)
        if refusal is not None:
            raise HTTPException(refusal[0], refusal[1])

    changing_pair = body.pair is not None and body.pair != account.pair
    changing_market = body.market_type is not None and body.market_type != account.market_type
    changing_selection_mode = body.pair_selection_mode is not None and body.pair_selection_mode != account.pair_selection_mode
    if changing_pair or changing_market:
        # Checked before the open-trade guard below, exactly as the schema
        # validators (422) always preceded it. Only a CHANGE is checked: an
        # unchanged selection is never revalidated, so nothing already in
        # place (including an open position's pair) is ever disturbed.
        problem = _pair_selection_problem(
            body.pair if body.pair is not None else account.pair,
            body.market_type if body.market_type is not None else account.market_type,
            allocated_capital=body.allocated_capital if body.allocated_capital is not None else account.allocated_capital,
            settings=settings,
            facts=facts,
        )
        if problem is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    if changing_pair or changing_market or changing_selection_mode:
        # Payment unlocks the account, not a pair - a paid customer can
        # switch pairs freely. What they can't do is switch out from
        # under a position the bot is still managing - and that applies
        # equally to handing pair selection over to/back from Auto Mode,
        # not just to the pair itself.
        _require_no_open_trade(db, account, action="changing pair, market type, or pair selection mode")
        if changing_pair:
            account.pair = body.pair
        if changing_market:
            account.market_type = body.market_type
        if changing_selection_mode:
            account.pair_selection_mode = body.pair_selection_mode
        # Stale readings from the old configuration shouldn't be shown
        # against the new one.
        account.last_signal = None
        account.last_signal_at = None
        account.last_scanned_at = None
        account.last_error = None
        account.last_error_at = None

    if body.risk_pct is not None:
        try:
            account.risk_pct = clamp_risk_pct(body.risk_pct, max_risk_pct=settings.risk_pct_max)  # the last guard
        except InvalidRiskParameters as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        for doc_type in ack_to_record:
            record_acceptance(db, customer_id=customer.id, document_type=doc_type, version=risk_limits.ACK_VERSION)
    if body.daily_loss_limit_usd is not None:
        # Lowering applies at once, a raise starts at the next midnight Lagos time (daily_limit.set_amount). Safe with a trade open: open trades keep their stops.
        today, tomorrow = _day_keys(settings)
        daily_limit.set_amount(account, body.daily_loss_limit_usd, today_key=today, next_day_key=tomorrow)
    if body.pair_list is not None:
        account.pair_list = ",".join(body.pair_list) or None  # the same owner-set column the bot already reads; at most 2 pairs of at most 9 characters fit its 80
    if body.live_slots is not None:
        account.live_slots = body.live_slots  # lowering never closes a trade; it only stops new entries until fewer are open
    if body.max_leverage is not None:
        # Applies to NEW entries only (the bot reads it when it sizes an entry), so it is safe to change with a trade open: that trade is not touched.
        try:
            account.max_leverage = leverage_module.validate(body.max_leverage, settings.futures_max_leverage)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if body.allocated_capital is not None:
        # Safe to change freely, even mid-trade - unlike pair/market/
        # credential, it only affects sizing at the *next* entry, never
        # how an already-open position is managed (same reasoning
        # risk_pct above has always been editable under).
        account.allocated_capital = body.allocated_capital
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


# --- GET /api/account/binance-settings: a read-only look at the leverage and margin mode Binance has for the account's pair ---
# Cached per account in this process so it cannot be used to hammer Binance: a good answer is reused for 60 seconds, a failed one for 10, and a request that arrives while another for the same
# account is still being answered gets a fixed "still checking" text without any exchange call. One read (the same function the trader uses), nothing is placed or changed.
_BS_OK_TTL, _BS_FAIL_TTL, _BS_PENDING_TTL = 60.0, 10.0, 10.0
_bs_cache: dict[str, tuple[float, "BinanceSettingsOut"]] = {}
_bs_pending: dict[str, float] = {}


def _bs_now() -> float:
    return time.monotonic()


def _bs_not_applicable(text: str) -> "BinanceSettingsOut":
    return BinanceSettingsOut(status="not_applicable", text=text)


@router.get("/binance-settings", response_model=BinanceSettingsOut)
async def get_binance_settings(
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BinanceSettingsOut:
    account = _require_account(customer)
    if _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings).mode != "live":
        return _bs_not_applicable(leverage_module.TXT_PAPER)
    if account.market_type != "futures":
        return _bs_not_applicable(leverage_module.TXT_SPOT)
    now = _bs_now()
    cached = _bs_cache.get(account.id)
    if cached is not None and cached[0] > now and cached[1].pair == account.pair:
        return cached[1]
    if _bs_pending.get(account.id, 0.0) > now:
        return BinanceSettingsOut(status="unavailable", pair=account.pair, text=leverage_module.TXT_BUSY)
    failed = BinanceSettingsOut(status="unavailable", pair=account.pair, text=leverage_module.TXT_FAILED)
    if not settings.encryption_key or account.credential is None:
        return failed
    _bs_pending[account.id] = now + _BS_PENDING_TTL
    result = failed
    client = None
    try:
        api_key = decrypt(account.credential.encrypted_api_key, key=settings.encryption_key)
        api_secret = decrypt(account.credential.encrypted_api_secret, key=settings.encryption_key)
        client = exchange.build_exchange_client(exchange="binance", market_type="futures", api_key=api_key, api_secret=api_secret, sandbox=False)
        leverage, mode = await exchange.fetch_symbol_leverage_and_margin_mode(client, exchange.exchange_symbol(account.pair, "futures"), side="long")
        text = leverage_module.describe(account.pair, leverage, mode)
        if text is not None:
            result = BinanceSettingsOut(status="ok", pair=account.pair, leverage=leverage, margin_mode=mode, text=text)
    except Exception:  # noqa: BLE001 - never the exchange's text, never a key: a fixed failure string
        result = failed
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        _bs_pending.pop(account.id, None)
    _bs_cache[account.id] = (_bs_now() + (_BS_OK_TTL if result.status == "ok" else _BS_FAIL_TTL), result)
    return result


# --- /api/account/advice: the setup advisor (docs/safety/setup_advisor.md). SETUP_ADVISOR off (default): the route does not exist (404). It reads this customer's own account only, uses data the service
# already holds (no exchange call), changes nothing and returns no secret. ---
def _advice(customer: Customer, db: Session, settings: Settings, body: AdviceRequest | None) -> AdviceOut:
    if not settings.setup_advisor:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    account = _require_account(customer)
    today, _ = _day_keys(settings)
    r = daily_limit.resolve(account.daily_loss_limit_usd, account.daily_loss_limit_pending_usd, account.daily_loss_limit_pending_from, today)
    cached_pairs = pair_catalog.cached_facts() or {}
    bs = _bs_cache.get(account.id)
    bs_out = bs[1] if (bs is not None and bs[0] > _bs_now() and bs[1].status == "ok") else None
    facts = setup_advice.Facts(
        switch_multi=settings.live_multi_position, risk_pct_max=settings.risk_pct_max, max_notional_multiplier=settings.max_notional_multiplier,
        futures_max_leverage=settings.futures_max_leverage, live_total_risk_cap_pct=settings.live_total_risk_cap_pct, donchian_risk_pct=settings.donchian_risk_pct,
        rsi_risk_pct=settings.rsi_mr_risk_pct, rsi_live=settings.live_rsi_mr, donchian_live=settings.live_donchian, daily_limit_now=r.amount,
        reference_value=daily_limit.reference_value(account, db),
        acks_on_file={t: has_current_acceptance(db, customer_id=customer.id, document_type=t, version=risk_limits.ACK_VERSION) for t in (risk_limits.ACK_HIGH, risk_limits.ACK_VERY_HIGH)},
        pair_min_notional={k: v.min_notional for k, v in cached_pairs.items() if v.listed and v.min_notional is not None},
        binance_leverage=bs_out.leverage if bs_out else None, binance_margin_mode=bs_out.margin_mode if bs_out else None, binance_pair=bs_out.pair if bs_out else None,
    )
    proposed = setup_advice.Proposed(**body.model_dump()) if body is not None else None
    return AdviceOut(notes=setup_advice.advise(account, facts, proposed))


@router.get("/advice", response_model=AdviceOut)
def get_advice(customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> AdviceOut:
    return _advice(customer, db, settings, None)


@router.post("/advice", response_model=AdviceOut)
def post_advice(body: AdviceRequest, customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> AdviceOut:
    return _advice(customer, db, settings, body)


@router.get("/risk-limits")
def get_risk_limits(customer: Customer = Depends(require_legal_acceptance), settings: Settings = Depends(get_settings)) -> dict:
    """What the risk-per-trade field may offer, readable before an account exists (onboarding): the highest value allowed right now and the two band edges."""
    return {"risk_pct_limit": settings.risk_pct_max, "recommended_max": risk_limits.RISK_RECOMMENDED_MAX, "very_high_from": risk_limits.RISK_VERY_HIGH_FROM}


@router.post("/total-loss-reset", response_model=AccountOut)
def reset_total_loss_stop(
    body: TotalLossResetRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    """The customer releases the total loss stop (docs/safety/risk_per_trade.md) after confirming. Never automatic. It also clears the reference value, so the next healthy
    cycle takes a fresh one from the account's then-current equity (this is also how a deposit or withdrawal is accounted for). It never touches an open trade."""
    account = _require_account(customer)
    account_loss.reset_total_stop(db, account, datetime.now(timezone.utc))
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


@router.post("/live-bypass", response_model=AccountOut)
def accept_live_trading_bypass(
    body: LiveBypassRequest, customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    account = _require_account(customer)
    if account.live_bypass_accepted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "live trading was already accepted for this account")
    account.live_bypass_accepted_at = accept_live_bypass()
    account.live_bypass_disclosure_text = LIVE_TRADING_DISCLOSURE
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


@router.put("/credentials", response_model=AccountOut)
def update_credentials(
    body: CredentialUpdateRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    account = _require_account(customer)
    if not settings.encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ENCRYPTION_KEY is not configured")
    if body.environment == "mainnet" and not _paid(customer, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "pay to unlock before connecting a mainnet Binance account")

    # Replacing the credential while a trade is open is exactly as unsafe
    # as switching pair/market mid-trade - the next cycle would try to
    # manage that position with a key that may not even be valid for it.
    _require_no_open_trade(db, account, action="changing your API credentials")

    credential = account.credential
    if credential is None:
        credential = ApiCredential(account_id=account.id, exchange="binance")
        db.add(credential)
    credential.environment = body.environment
    credential.encrypted_api_key = encrypt(body.api_key, key=settings.encryption_key)
    credential.encrypted_api_secret = encrypt(body.api_secret, key=settings.encryption_key)
    # A replaced credential is the one thing that lifts the withdrawal-permission
    # block (the new key is validated afresh on its first entry).
    account.withdrawal_blocked_at = None

    # A credential that's no longer mainnet-tagged can't back live
    # trading - turn the switch back off explicitly rather than leaving
    # it set and relying only on resolve_trading_mode's own re-check
    # (which still applies regardless, as a second independent guard).
    if body.environment != "mainnet" and account.live_trading_enabled_at is not None:
        account.live_trading_enabled_at = None
        account.live_trading_disclosure_text = None
        # A "started" bot with no valid authorization backing it is a
        # stale, confusing state to leave lying around - clear it too,
        # same reasoning as live_trading_enabled_at above. The mode gate
        # (resolve_trading_mode) already fails closed regardless of
        # whether this cleanup happens; this is just hygiene, not a
        # second safety net this specific line is load-bearing for.
        account.bot_started_at = None

    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


@router.post("/enable-live-trading", response_model=AccountOut)
def enable_live_trading_route(
    body: EnableLiveTradingRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    """The one and only action that can set live_trading_enabled_at -
    paying, finishing the trial, and accepting the bypass are all
    eligibility, never activation. See onboarding.resolve_trading_mode.
    The four existing safety gates below are unchanged; the risk
    disclosure acknowledgment (`body.accept_risk_disclosure`, already
    guaranteed True by EnableLiveTradingRequest's validator, same as
    SignupRequest's) is a FIFTH, additional requirement, not a
    replacement for any of them.

    Every check below runs and can raise before any database write, so
    a failed check never leaves a risk-disclosure acceptance record on
    file for an activation that didn't happen - the account mutation and
    the acceptance row are only ever written together, right at the
    end, inside this same request's transaction (see db.session_scope,
    which commits once at the end of the request and rolls back
    everything written so far if anything above raises)."""
    account = _require_account(customer)

    if body.risk_disclosure_version is not None and body.risk_disclosure_version != RISK_DISCLOSURE_VERSION:
        raise HTTPException(status.HTTP_409_CONFLICT, "the Risk Disclosure was updated - reload the page and read it again before enabling live trading")

    if not _paid(customer, settings):
        raise HTTPException(status.HTTP_409_CONFLICT, "pay to unlock live trading before enabling it")

    eligible = is_live_eligible(trial_ends_at=account.trial_ends_at, live_bypass_accepted_at=account.live_bypass_accepted_at)
    if not eligible:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this account hasn't finished its trial yet - wait for it to end, or accept the trial bypass first",
        )

    if not account.has_mainnet_credential:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "add a Binance API key explicitly tagged \"mainnet\" before enabling live trading - a testnet "
            "credential can never be used for this",
        )

    if account.live_trading_enabled_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "live trading is already enabled for this account")

    if account.demo_enabled_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, demo_module.MSG_LIVE_BLOCKED)

    # Enabling live trading while a (paper) trade is open is its own
    # hazard: the very next cycle could resolve to LIVE and try to
    # manage that paper position with a real production order against a
    # position that only exists in our own simulated ledger.
    _require_no_open_trade(db, account, action="enabling live trading")

    if account.daily_loss_limit_usd is None:
        raise HTTPException(status.HTTP_409_CONFLICT, DAILY_LIMIT_REQUIRED_TEXT)

    account.live_trading_enabled_at = enable_live_trading()
    account.live_trading_disclosure_text = LIVE_TRADING_ACTIVATION_DISCLOSURE
    if not has_current_acceptance(db, customer_id=customer.id, document_type="risk_disclosure", version=RISK_DISCLOSURE_VERSION):
        record_acceptance(db, customer_id=customer.id, document_type="risk_disclosure", version=RISK_DISCLOSURE_VERSION)
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


@router.post("/start-bot", response_model=AccountOut)
def start_bot_route(
    body: StartBotRequest, customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    """The final, separate action - enabling live trading never starts
    anything by itself (see enable_live_trading_route above); this is
    the one and only thing that can set bot_started_at, which
    resolve_trading_mode's fifth gate requires before it will ever
    return LIVE. Every prerequisite is re-checked here independently of
    whatever already got this account to live_trading_enabled_at, same
    "check everything before any write" discipline as that endpoint."""
    account = _require_account(customer)

    if account.withdrawal_blocked_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "your Binance API key has withdrawal permission enabled - replace it with a trade-only key before starting the bot",
        )

    if not _paid(customer, settings):
        raise HTTPException(status.HTTP_409_CONFLICT, "pay to unlock before starting the bot")

    eligible = is_live_eligible(trial_ends_at=account.trial_ends_at, live_bypass_accepted_at=account.live_bypass_accepted_at)
    if not eligible:
        raise HTTPException(status.HTTP_409_CONFLICT, "this account is not yet eligible for live trading")

    if not account.has_mainnet_credential:
        raise HTTPException(status.HTTP_409_CONFLICT, "connect a mainnet Binance account before starting the bot")

    if account.live_trading_enabled_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "enable live trading and accept the risk disclosure before starting the bot")

    if account.allocated_capital is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "configure your trading capital before starting the bot")

    if account.bot_started_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the bot is already started for this account")

    if account.demo_enabled_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, demo_module.MSG_LIVE_BLOCKED)

    _require_no_open_trade(db, account, action="starting the bot")

    if account.daily_loss_limit_usd is None:
        raise HTTPException(status.HTTP_409_CONFLICT, DAILY_LIMIT_REQUIRED_TEXT)

    account.bot_started_at = start_bot()
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


async def _demo_keys_accepted(account: TradingAccount, api_key: str, api_secret: str) -> bool:
    """One authenticated read against the DEMO endpoint (a client built with sandbox=True and proven to route only to the demo system). False for any failure: a real key, a key for the other
    demo system, a bad key, an unreachable service. Never returns or logs the exchange's text."""
    client = None
    try:
        client = exchange.build_exchange_client(exchange="binance", market_type=account.market_type, api_key=api_key, api_secret=api_secret, sandbox=True)
        await client.fetch_balance()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


def _require_demo_runner(settings: Settings) -> None:
    if not settings.demo_runner:
        raise HTTPException(status.HTTP_409_CONFLICT, demo_module.MSG_OFF)


@router.put("/demo-credentials", response_model=AccountOut)
async def put_demo_credentials(
    body: DemoCredentialsRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    """Store the customer's DEMO keys (separate from the live credential). Refused unless Binance accepts them on the demo endpoint; refused outright if they equal the stored live keys."""
    _require_demo_runner(settings)
    account = _require_account(customer)
    if not settings.encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ENCRYPTION_KEY is not configured")
    if account.live_trading_enabled_at is not None or account.bot_started_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, demo_module.MSG_LIVE_ACTIVE)
    _require_no_open_trade(db, account, action="changing your demo keys")
    if account.credential is not None:
        try:
            if body.api_key == decrypt(account.credential.encrypted_api_key, key=settings.encryption_key):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, demo_module.MSG_SAME_AS_LIVE)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - an unreadable stored key cannot be compared; the demo-endpoint read below is the real test
            pass
    if not await _demo_keys_accepted(account, body.api_key, body.api_secret):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, demo_module.MSG_KEYS_REFUSED)
    cred = account.demo_credential
    if cred is None:
        cred = DemoCredential(account_id=account.id, encrypted_api_key="", encrypted_api_secret="")
        db.add(cred)
    cred.encrypted_api_key = encrypt(body.api_key, key=settings.encryption_key)
    cred.encrypted_api_secret = encrypt(body.api_secret, key=settings.encryption_key)
    db.flush()
    db.refresh(account)
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


@router.post("/demo-mode", response_model=AccountOut)
async def set_demo_mode(
    body: DemoModeRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    """Switch DEMO mode on or off. On needs: the service switch, demo keys on file that Binance accepts on the demo endpoint right now, no live trading enabled or started, trading capital set and no
    open trade. Off needs no open trade (a position is never stranded). Live trading is refused while DEMO is on."""
    account = _require_account(customer)
    if body.enabled:
        _require_demo_runner(settings)
        if account.live_trading_enabled_at is not None or account.bot_started_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, demo_module.MSG_LIVE_ACTIVE)
        key, secret = demo_module.credentials(account, settings)
        if key is None:
            raise HTTPException(status.HTTP_409_CONFLICT, demo_module.MSG_NEED_KEYS)
        if account.allocated_capital is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "configure your trading capital before switching demo mode on")
        _require_no_open_trade(db, account, action="switching demo mode on")
        if not await _demo_keys_accepted(account, key, secret):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, demo_module.MSG_KEYS_REFUSED)
        if account.demo_enabled_at is None:
            account.demo_enabled_at = datetime.now(timezone.utc)
    else:
        _require_no_open_trade(db, account, action="switching demo mode off")
        account.demo_enabled_at = None
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


@router.delete("/demo-credentials", response_model=AccountOut)
def delete_demo_credentials(
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    """Remove the demo keys. Demo mode is switched off with them. Refused while a trade is open."""
    account = _require_account(customer)
    _require_no_open_trade(db, account, action="removing your demo keys")
    account.demo_enabled_at = None
    if account.demo_credential is not None:
        db.delete(account.demo_credential)
        db.flush()
        db.refresh(account)
    return _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings)


def _paper_credentials(account: TradingAccount, settings: Settings) -> tuple[str | None, str | None]:
    """Mirrors trader.py's `_resolve_credentials` paper-mode branch
    exactly (same spot/futures split) - duplicated rather than imported
    because trader.py's version is a private helper scoped to the
    trading cycle, and this read-only endpoint has no business pulling
    in trader.py's module surface for two lines of logic."""
    if account.market_type == "futures":
        return settings.demo_futures_api_key, settings.demo_futures_api_secret
    return settings.testnet_spot_api_key, settings.testnet_spot_api_secret


@router.get("/balance", response_model=BalanceOut)
async def get_balance(
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BalanceOut:
    """Live Binance balance - only ever meaningful once this account is
    actually LIVE (reuses the exact same mode computation as every other
    account response, never a separate check that could drift)."""
    account = _require_account(customer)
    mode = _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings).mode
    if mode not in ("live", "demo"):
        raise HTTPException(status.HTTP_409_CONFLICT, "balance is only available once live trading is enabled")
    if mode == "demo":
        api_key, api_secret = demo_module.credentials(account, settings)  # the customer's demo keys, on the demo endpoint only
        if api_key is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no usable demo keys are configured")
    else:
        if not settings.encryption_key or account.credential is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no usable trading credentials are configured")
        api_key = decrypt(account.credential.encrypted_api_key, key=settings.encryption_key)
        api_secret = decrypt(account.credential.encrypted_api_secret, key=settings.encryption_key)
    client = exchange.build_exchange_client(
        exchange="binance", market_type=account.market_type, api_key=api_key, api_secret=api_secret, sandbox=(mode == "demo")
    )
    try:
        available = await exchange.fetch_available_quote_balance(client, account.pair)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exchange.safe_error_message(exc)) from exc
    finally:
        await client.close()
    return BalanceOut(quote_currency=exchange.quote_currency(account.pair), available_balance=available)


@router.get("/position", response_model=PositionOut)
async def get_position(
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PositionOut:
    """The account's open position (if any) with a live current price
    layered on top. Never places an order - a single read-only ticker
    fetch, same credential selection a real cycle would use for this
    account's current mode. A failed price fetch degrades to
    `current_price`/`unrealized_pnl` of None rather than a 5xx, since a
    dashboard shouldn't break just because one ticker call was slow."""
    account = _require_account(customer)
    open_trade = get_open_trade(db, account.id)
    if open_trade is None:
        return PositionOut(has_position=False)

    is_live = _to_account_out(account, has_paid=_paid(customer, settings), db=db, settings=settings).mode == "live"
    current_price: float | None = None
    try:
        if is_live:
            if not settings.encryption_key or account.credential is None:
                raise RuntimeError("no usable live credentials")
            api_key = decrypt(account.credential.encrypted_api_key, key=settings.encryption_key)
            api_secret = decrypt(account.credential.encrypted_api_secret, key=settings.encryption_key)
        else:
            api_key, api_secret = _paper_credentials(account, settings)
            if api_key is None:
                raise RuntimeError("no usable testnet credentials")

        client = exchange.build_exchange_client(
            exchange="binance",
            market_type=account.market_type,
            api_key=api_key,
            api_secret=api_secret,
            sandbox=not is_live,
        )
        try:
            ticker = await client.fetch_ticker(
                exchange.exchange_symbol(open_trade.pair or account.pair, account.market_type)
            )
            current_price = ticker.get("last") or ticker.get("close")
        finally:
            await client.close()
    except Exception:
        current_price = None  # dashboard still shows the position itself, just without a live price

    unrealized_pnl = None
    if current_price is not None:
        if open_trade.side == "long":
            unrealized_pnl = (current_price - open_trade.entry_price) * open_trade.quantity
        else:
            unrealized_pnl = (open_trade.entry_price - current_price) * open_trade.quantity

    return PositionOut(
        has_position=True,
        pair=open_trade.pair or account.pair,
        side=open_trade.side,
        is_paper=open_trade.is_paper,
        entry_price=open_trade.entry_price,
        current_price=current_price,
        quantity=open_trade.quantity,
        stop_loss_price=open_trade.stop_loss_price,
        unrealized_pnl=unrealized_pnl,
        opened_at=open_trade.opened_at,
        protected=None if open_trade.is_paper else open_trade.protection_status == PROTECTION_PROTECTED,
    )


@router.get("/positions", response_model=OpenTradesOut)
def get_positions(customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db)) -> OpenTradesOut:
    """Every non-terminal trade of the account (database only; no exchange call). `/position` keeps describing the oldest one, as
    before. A normal account has zero or one; a paper multi-trade test account up to three."""
    account = _require_account(customer)
    rows = list_open_trades(db, account.id)
    return OpenTradesOut(
        open_count=len(rows),
        trades=[
            OpenTradeOut(
                id=t.id, pair=t.pair or account.pair, side=t.side, status=t.status, is_paper=t.is_paper, strategy_id=strategy_of(t),
                entry_price=t.entry_price, quantity=t.quantity, stop_loss_price=t.stop_loss_price, opened_at=t.opened_at,
                protected=None if t.is_paper else t.protection_status == PROTECTION_PROTECTED,
            )
            for t in rows
        ],
    )


@router.post("/credentials/test", response_model=CredentialTestOut)
async def test_credentials(
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CredentialTestOut:
    """Verifies the currently stored credential actually works, without
    running a full trading cycle and without ever placing an order -
    one read-only `fetch_balance` call against whichever environment
    (testnet/mainnet) the credential is tagged for."""
    account = _require_account(customer)
    if account.credential is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no credential is stored yet")
    if not settings.encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ENCRYPTION_KEY is not configured")

    api_key = decrypt(account.credential.encrypted_api_key, key=settings.encryption_key)
    api_secret = decrypt(account.credential.encrypted_api_secret, key=settings.encryption_key)
    sandbox = account.credential.environment != "mainnet"
    try:
        client = exchange.build_exchange_client(
            exchange="binance", market_type=account.market_type, api_key=api_key, api_secret=api_secret, sandbox=sandbox
        )
    except exchange.NonProductionRoutingError as exc:
        return CredentialTestOut(ok=False, message=exchange.safe_error_message(exc))
    try:
        await client.fetch_balance()
    except Exception as exc:
        return CredentialTestOut(ok=False, message=exchange.safe_error_message(exc))
    finally:
        await client.close()
    return CredentialTestOut(ok=True, message="Connection verified.")
