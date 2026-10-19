"""The customer's trading account: onboarding, settings, pause/resume,
and the live-trading bypass - all reusing the same `onboarding.py` and
`crypto_utils.py` functions the CLI signup script uses, not a parallel
implementation of the trial/bypass rules.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ... import exchange
from ...botcode import assign_unique_bot_code
from ...config import Settings
from ...crypto_utils import decrypt, encrypt
from ...db import get_open_trade
from ...models import PROTECTION_PROTECTED, TRADE_ATTENTION
from ...legal import RISK_DISCLOSURE_VERSION, record_acceptance
from ...models import ApiCredential, Customer, TradingAccount
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
from ..deps import get_db, get_settings, require_legal_acceptance
from ..schemas import (
    AccountCreateRequest,
    AccountOut,
    AccountUpdateRequest,
    BalanceOut,
    CredentialTestOut,
    CredentialUpdateRequest,
    EnableLiveTradingRequest,
    LiveBypassRequest,
    PositionOut,
    StartBotRequest,
)

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
        bot_started_at=account.bot_started_at,
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
    elif (active_trade := get_open_trade(db, account.id)) is not None and active_trade.status == TRADE_ATTENTION:
        # A live position the bot cannot currently vouch for (unprotected,
        # unresolved entry, residual exposure, ...). Takes precedence over
        # "error": it is the more specific, more urgent condition.
        runtime_status = "attention"
    elif account.withdrawal_blocked_at is not None and active_trade is None:
        # No position, and the stored key can withdraw: trading is blocked
        # until the credential is replaced.
        runtime_status = "error"
    elif account.last_error is not None:
        runtime_status = "error"
    elif active_trade is not None:
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
        bot_started_at=account.bot_started_at,
        allocated_capital=account.allocated_capital,
        pair_selection_mode=account.pair_selection_mode,
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
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    if customer.account is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this customer already has a trading account")
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
    if body.environment == "mainnet" and not customer.has_paid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "pay to unlock before connecting a mainnet Binance account")

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
    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.get("", response_model=AccountOut)
def get_account(customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db)) -> AccountOut:
    return _to_account_out(_require_account(customer), has_paid=customer.has_paid, db=db)


@router.patch("", response_model=AccountOut)
def update_account(
    body: AccountUpdateRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    account = _require_account(customer)

    changing_pair = body.pair is not None and body.pair != account.pair
    changing_market = body.market_type is not None and body.market_type != account.market_type
    changing_selection_mode = body.pair_selection_mode is not None and body.pair_selection_mode != account.pair_selection_mode
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
            account.risk_pct = clamp_risk_pct(body.risk_pct, max_risk_pct=settings.risk_pct_max)
        except InvalidRiskParameters as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if body.allocated_capital is not None:
        # Safe to change freely, even mid-trade - unlike pair/market/
        # credential, it only affects sizing at the *next* entry, never
        # how an already-open position is managed (same reasoning
        # risk_pct above has always been editable under).
        account.allocated_capital = body.allocated_capital
    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.post("/live-bypass", response_model=AccountOut)
def accept_live_trading_bypass(
    body: LiveBypassRequest, customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db)
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
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccountOut:
    account = _require_account(customer)
    if not settings.encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ENCRYPTION_KEY is not configured")
    if body.environment == "mainnet" and not customer.has_paid:
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

    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.post("/enable-live-trading", response_model=AccountOut)
def enable_live_trading_route(
    body: EnableLiveTradingRequest,
    customer: Customer = Depends(require_legal_acceptance),
    db: Session = Depends(get_db),
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
    record_acceptance(db, customer_id=customer.id, document_type="risk_disclosure", version=RISK_DISCLOSURE_VERSION)
    return _to_account_out(account, has_paid=customer.has_paid, db=db)


@router.post("/start-bot", response_model=AccountOut)
def start_bot_route(
    body: StartBotRequest, customer: Customer = Depends(require_legal_acceptance), db: Session = Depends(get_db)
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

    if not customer.has_paid:
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

    _require_no_open_trade(db, account, action="starting the bot")

    account.bot_started_at = start_bot()
    return _to_account_out(account, has_paid=customer.has_paid, db=db)


def _paper_credentials(account: TradingAccount, settings: Settings) -> tuple[str | None, str | None]:
    """Mirrors trader.py's `_resolve_credentials` paper-mode branch
    exactly (same spot/futures split) - duplicated rather than imported
    because trader.py's version is a private helper scoped to the
    trading cycle, and this read-only endpoint has no business pulling
    in trader.py's module surface for two lines of logic."""
    if account.market_type == "futures":
        return settings.testnet_futures_api_key, settings.testnet_futures_api_secret
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
    if _to_account_out(account, has_paid=customer.has_paid, db=db).mode != "live":
        raise HTTPException(status.HTTP_409_CONFLICT, "balance is only available once live trading is enabled")
    if not settings.encryption_key or account.credential is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no usable trading credentials are configured")

    api_key = decrypt(account.credential.encrypted_api_key, key=settings.encryption_key)
    api_secret = decrypt(account.credential.encrypted_api_secret, key=settings.encryption_key)
    client = exchange.build_exchange_client(
        exchange="binance", market_type=account.market_type, api_key=api_key, api_secret=api_secret, sandbox=False
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

    is_live = _to_account_out(account, has_paid=customer.has_paid, db=db).mode == "live"
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
    client = exchange.build_exchange_client(
        exchange="binance", market_type=account.market_type, api_key=api_key, api_secret=api_secret, sandbox=sandbox
    )
    try:
        await client.fetch_balance()
    except Exception as exc:
        return CredentialTestOut(ok=False, message=exchange.safe_error_message(exc))
    finally:
        await client.close()
    return CredentialTestOut(ok=True, message="Connection verified.")
