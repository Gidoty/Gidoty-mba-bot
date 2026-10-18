"""The customer's trading account: onboarding, settings, pause/resume,
and the live-trading bypass - all reusing the same `onboarding.py` and
`crypto_utils.py` functions the CLI signup script uses, not a parallel
implementation of the trial/bypass rules.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...botcode import assign_unique_bot_code
from ...config import Settings
from ...crypto_utils import encrypt
from ...db import get_open_trade
from ...models import ApiCredential, Customer, TradingAccount
from ...onboarding import (
    LIVE_TRADING_ACTIVATION_DISCLOSURE,
    LIVE_TRADING_DISCLOSURE,
    accept_live_bypass,
    enable_live_trading,
    is_live_eligible,
    resolve_trading_mode,
    start_trial,
)
from ...risk import InvalidRiskParameters, clamp_risk_pct
from ..deps import get_current_customer, get_db, get_settings
from ..schemas import AccountCreateRequest, AccountOut, AccountUpdateRequest, CredentialUpdateRequest, LiveBypassRequest

router = APIRouter(prefix="/api/account", tags=["account"])


def _require_account(customer: Customer) -> TradingAccount:
    if customer.account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet - POST /api/account first")
    return customer.account


def _to_account_out(account: TradingAccount, *, has_paid: bool, db: Session) -> AccountOut:
    state = resolve_trading_mode(
        trial_ends_at=account.trial_ends_at,
        live_bypass_accepted_at=account.live_bypass_accepted_at,
        has_paid=has_paid,
        live_trading_enabled_at=account.live_trading_enabled_at,
        has_mainnet_credential=account.has_mainnet_credential,
    )

    # Priority order: a customer's own pause always wins (nothing else
    # matters if they turned it off themselves); an error is the most
    # actionable thing to surface next, even if a trade happens to be
    # open; otherwise an open trade is worth calling out over the
    # default "scanning" state. "error" is never sticky past the
    # problem - trader.py clears last_error on the next healthy cycle.
    if not account.is_active:
        runtime_status = "paused"
    elif account.last_error is not None:
        runtime_status = "error"
    elif get_open_trade(db, account.id) is not None:
        runtime_status = "trade_open"
    else:
        runtime_status = "active"

    return AccountOut(
        id=account.id,
        bot_code=account.bot_code,
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
        runtime_status=runtime_status,
        last_scanned_at=account.last_scanned_at,
        last_signal=account.last_signal,
        last_signal_at=account.last_signal_at,
        last_error=account.last_error,
        last_error_at=account.last_error_at,
    )


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

    db.add(
        ApiCredential(
            account_id=account.id,
            exchange="binance",
            environment=body.environment,
            encrypted_api_key=encrypt(body.api_key, key=settings.encryption_key),
            encrypted_api_secret=encrypt(body.api_secret, key=settings.encryption_key),
        )
    )
    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.get("", response_model=AccountOut)
def get_account(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> AccountOut:
    return _to_account_out(_require_account(customer), has_paid=customer.has_paid, db=db)


@router.patch("", response_model=AccountOut)
def update_account(
    body: AccountUpdateRequest,
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    account = _require_account(customer)

    changing_pair = body.pair is not None and body.pair != account.pair
    changing_market = body.market_type is not None and body.market_type != account.market_type
    if changing_pair or changing_market:
        # Payment unlocks the account, not a pair - a paid customer can
        # switch pairs freely. What they can't do is switch out from
        # under a position the bot is still managing.
        _require_no_open_trade(db, account, action="switching pair or market type")
        if changing_pair:
            account.pair = body.pair
        if changing_market:
            account.market_type = body.market_type
        # Stale readings from the old configuration shouldn't be shown
        # against the new one.
        account.last_signal = None
        account.last_signal_at = None
        account.last_scanned_at = None
        account.last_error = None
        account.last_error_at = None

    if body.risk_pct is not None:
        try:
            account.risk_pct = clamp_risk_pct(body.risk_pct, max_risk_pct=settings.risk_pct_max)
        except InvalidRiskParameters as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if body.is_active is not None:
        account.is_active = body.is_active
    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.post("/live-bypass", response_model=AccountOut)
def accept_live_trading_bypass(
    body: LiveBypassRequest, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)
) -> AccountOut:
    account = _require_account(customer)
    if account.live_bypass_accepted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "live trading was already accepted for this account")
    account.live_bypass_accepted_at = accept_live_bypass()
    account.live_bypass_disclosure_text = LIVE_TRADING_DISCLOSURE
    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.put("/credentials", response_model=AccountOut)
def update_credentials(
    body: CredentialUpdateRequest,
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    account = _require_account(customer)
    if not settings.encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ENCRYPTION_KEY is not configured")

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

    # A credential that's no longer mainnet-tagged can't back live
    # trading - turn the switch back off explicitly rather than leaving
    # it set and relying only on resolve_trading_mode's own re-check
    # (which still applies regardless, as a second independent guard).
    if body.environment != "mainnet" and account.live_trading_enabled_at is not None:
        account.live_trading_enabled_at = None
        account.live_trading_disclosure_text = None

    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.post("/enable-live-trading", response_model=AccountOut)
def enable_live_trading_route(
    body: LiveBypassRequest, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)
) -> AccountOut:
    """The one and only action that can set live_trading_enabled_at -
    paying, finishing the trial, and accepting the bypass are all
    eligibility, never activation. See onboarding.resolve_trading_mode."""
    account = _require_account(customer)

    if not customer.has_paid:
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

    # Enabling live trading while a (paper) trade is open is its own
    # hazard: the very next cycle could resolve to LIVE and try to
    # manage that paper position with a real production order against a
    # position that only exists in our own simulated ledger.
    _require_no_open_trade(db, account, action="enabling live trading")

    account.live_trading_enabled_at = enable_live_trading()
    account.live_trading_disclosure_text = LIVE_TRADING_ACTIVATION_DISCLOSURE
    return _to_account_out(account, has_paid=customer.has_paid, db=db)
