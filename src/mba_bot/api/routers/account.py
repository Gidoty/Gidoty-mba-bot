"""The customer's trading account: onboarding, settings, pause/resume,
and the live-trading bypass - all reusing the same `onboarding.py` and
`crypto_utils.py` functions the CLI signup script uses, not a parallel
implementation of the trial/bypass rules.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...config import Settings
from ...crypto_utils import encrypt
from ...models import ApiCredential, Customer, TradingAccount
from ...onboarding import LIVE_TRADING_DISCLOSURE, accept_live_bypass, resolve_trading_mode, start_trial
from ...risk import InvalidRiskParameters, clamp_risk_pct
from ..deps import get_current_customer, get_db, get_settings
from ..schemas import AccountCreateRequest, AccountOut, AccountUpdateRequest, LiveBypassRequest

router = APIRouter(prefix="/api/account", tags=["account"])


def _require_account(customer: Customer) -> TradingAccount:
    if customer.account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet - POST /api/account first")
    return customer.account


def _to_account_out(account: TradingAccount) -> AccountOut:
    state = resolve_trading_mode(trial_ends_at=account.trial_ends_at, live_bypass_accepted_at=account.live_bypass_accepted_at)
    return AccountOut(
        id=account.id,
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
    )


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    body: AccountCreateRequest,
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    if customer.account is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this customer already has a trading account")
    if body.notification_channel == "whatsapp":
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "WhatsApp notifications aren't available yet - use telegram")
    if not settings.encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ENCRYPTION_KEY is not configured")

    try:
        risk_pct = clamp_risk_pct(body.risk_pct or settings.risk_pct_default, max_risk_pct=settings.risk_pct_max)
    except InvalidRiskParameters as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    account = TradingAccount(
        customer_id=customer.id,
        pair=body.pair,
        market_type=body.market_type,
        notification_channel=body.notification_channel,
        risk_pct=risk_pct,
        trial_ends_at=start_trial(trial_days=settings.trial_days),
        paper_balance=settings.paper_starting_balance,
    )
    db.add(account)
    db.flush()

    db.add(
        ApiCredential(
            account_id=account.id,
            exchange="binance",
            encrypted_api_key=encrypt(body.api_key, key=settings.encryption_key),
            encrypted_api_secret=encrypt(body.api_secret, key=settings.encryption_key),
        )
    )
    return _to_account_out(account)


@router.get("", response_model=AccountOut)
def get_account(customer: Customer = Depends(get_current_customer)) -> AccountOut:
    return _to_account_out(_require_account(customer))


@router.patch("", response_model=AccountOut)
def update_account(
    body: AccountUpdateRequest,
    customer: Customer = Depends(get_current_customer),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    account = _require_account(customer)
    if body.risk_pct is not None:
        try:
            account.risk_pct = clamp_risk_pct(body.risk_pct, max_risk_pct=settings.risk_pct_max)
        except InvalidRiskParameters as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if body.is_active is not None:
        account.is_active = body.is_active
    return _to_account_out(account)


@router.post("/live-bypass", response_model=AccountOut)
def accept_live_trading_bypass(body: LiveBypassRequest, customer: Customer = Depends(get_current_customer)) -> AccountOut:
    account = _require_account(customer)
    if account.live_bypass_accepted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "live trading was already accepted for this account")
    account.live_bypass_accepted_at = accept_live_bypass()
    account.live_bypass_disclosure_text = LIVE_TRADING_DISCLOSURE
    return _to_account_out(account)
