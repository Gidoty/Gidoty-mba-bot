"""ccxt client construction and a runtime permission safety check.

Uses `ccxt.async_support` (not the sync `ccxt` module) so the shared
scheduler loop can hold many customers' exchange connections concurrently
in one process without threads - see ARCHITECTURE.md for why that's the
recommended deployment shape here.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from urllib.parse import urlparse

import ccxt.async_support as ccxt_async

from .risk import MaintenanceTier

logger = logging.getLogger(__name__)

_EXCHANGE_CLASSES = {
    ("binance", "spot"): ccxt_async.binance,
    ("binance", "futures"): ccxt_async.binanceusdm,
}


class UnsupportedExchange(ValueError):
    pass


class WithdrawalPermissionDetected(RuntimeError):
    """Stored keys must be trade-only. Raised if a runtime check finds a
    key that can withdraw - trading is refused for that account until the
    key is replaced, even if trade-only was intended at signup."""


class WithdrawalPermissionUnverifiable(RuntimeError):
    """Binance's API-key permission response was unavailable, malformed, or did
    not state whether withdrawals are enabled, so the key cannot be shown to be
    trade-only. Raised only on the ENTRY path (fail closed for that cycle). It
    is a verification failure, not a finding about the key: it never sets the
    durable withdrawal block and the next cycle simply asks again."""


class NonProductionRoutingError(RuntimeError):
    """A non-production (paper) client could not be proven to route only to
    its non-production Binance environment, or a production client was found
    routing to one. No client is returned: this fails closed and never falls
    back to another environment."""


# --- Environments -------------------------------------------------------------------
#
# sandbox=False                 Binance production (Mainnet) - Spot and USD-M.
# sandbox=True, market "spot"   Binance Spot Testnet (testnet.binance.vision)
#                               via ccxt's sandbox mode. Unchanged.
# sandbox=True, market "futures"
#                               Binance Futures DEMO TRADING (demo-fapi.binance.com)
#                               via ccxt's demo-trading mode. The legacy Futures
#                               Testnet (testnet.binancefuture.com) is NOT used:
#                               ccxt 4.5.88 refuses every authenticated call on it
#                               (NotSupported) and Binance has deprecated it. The two
#                               ccxt modes are mutually exclusive.
#
# Every client is verified after construction (assert_routing): the effective
# ccxt URL table - the same table sign() reads for every request - must contain
# only hosts of the intended environment. Demo and Spot-Testnet tables have
# no entries for endpoint groups they don't offer (sapi, papi, eapi...), so a
# request to one of those raises NotSupported instead of reaching Mainnet.
SPOT_TESTNET_HOST = "testnet.binance.vision"
# Present in ccxt's Spot sandbox table for the Futures endpoint groups a Spot
# client never uses; it is the deprecated legacy host, accepted ONLY as an
# unused entry of a Spot client's table, never for a Futures client.
_LEGACY_FUTURES_TESTNET_HOST = "testnet.binancefuture.com"
FUTURES_DEMO_HOSTS = frozenset({"demo-fapi.binance.com", "demo-api.binance.com", "demo-dapi.binance.com"})
# Endpoint groups the Spot client actually uses ("public", "private", "v1").
_SPOT_USED_API_GROUPS = ("public", "private", "v1")


def _routing_hosts(client) -> dict[str, str]:
    """api-group -> host for every entry of the client's effective URL table."""
    table = client.urls.get("api") if isinstance(getattr(client, "urls", None), dict) else None
    if not isinstance(table, dict) or not table:
        raise NonProductionRoutingError("exchange client has no readable URL table")
    hosts: dict[str, str] = {}
    for group, url in table.items():
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc:
            raise NonProductionRoutingError(f"unreadable or non-https endpoint for group {group!r}")
        hosts[str(group)] = parsed.netloc.lower()
    return hosts


def assert_routing(client, *, market_type: str, sandbox: bool) -> None:
    """Prove, from the effective URL table, that this client routes only where
    it should. Raises NonProductionRoutingError otherwise."""
    hosts = _routing_hosts(client)
    values = set(hosts.values())
    options = getattr(client, "options", {}) or {}
    demo_flag = bool(options.get("enableDemoTrading"))
    sandbox_flag = bool(getattr(client, "isSandboxModeEnabled", False))

    if not sandbox:
        bad = sorted(h for h in values if "demo" in h or "testnet" in h)
        if bad or demo_flag or sandbox_flag:
            raise NonProductionRoutingError("a production client is routed to a non-production environment")
        return

    if market_type == "futures":
        if not values <= FUTURES_DEMO_HOSTS:
            raise NonProductionRoutingError("a Futures paper client has an endpoint outside Binance Demo Trading")
        if not demo_flag or sandbox_flag:
            raise NonProductionRoutingError("a Futures paper client is not in demo-trading mode")
        if "fapiPrivate" not in hosts or hosts["fapiPrivate"] != "demo-fapi.binance.com":
            raise NonProductionRoutingError("a Futures paper client has no demo Futures endpoint")
        return

    # Spot Testnet (unchanged behaviour): the groups a Spot client uses must be the Spot Testnet
    # host; the table's remaining entries must be non-production hosts too, never a production one.
    if not values <= {SPOT_TESTNET_HOST, _LEGACY_FUTURES_TESTNET_HOST}:
        raise NonProductionRoutingError("a Spot paper client has an endpoint outside the Spot Testnet")
    if any(hosts.get(group) != SPOT_TESTNET_HOST for group in _SPOT_USED_API_GROUPS):
        raise NonProductionRoutingError("a Spot paper client is not routed to the Spot Testnet")
    if not sandbox_flag or demo_flag:
        raise NonProductionRoutingError("a Spot paper client is not in sandbox mode")


def build_exchange_client(
    *, exchange: str, market_type: str, api_key: str, api_secret: str, sandbox: bool
) -> ccxt_async.Exchange:
    exchange_cls = _EXCHANGE_CLASSES.get((exchange, market_type))
    if exchange_cls is None:
        raise UnsupportedExchange(f"no ccxt client configured for exchange={exchange!r} market_type={market_type!r}")

    client = exchange_cls({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    if sandbox:
        try:
            if market_type == "futures":
                client.enable_demo_trading(True)  # Binance Futures Demo Trading; NOT set_sandbox_mode
            else:
                client.set_sandbox_mode(True)  # Binance Spot Testnet
        except Exception as exc:
            raise NonProductionRoutingError(
                f"could not establish the non-production environment ({type(exc).__name__})"
            ) from None
    assert_routing(client, market_type=market_type, sandbox=sandbox)
    return client


# Shown to the customer (Telegram and dashboard) when a stored key can withdraw.
# Deliberately says what still happens to an existing position: the bot keeps
# managing it, for risk reduction only. No exchange text, no secrets.
WITHDRAWAL_BLOCK_MESSAGE = (
    "Your Binance API key currently has withdrawal permission enabled. No new trades will be opened. "
    "Any existing position will continue to be managed for risk reduction only. Replace the API key with one "
    "that has withdrawal permission disabled before new trading can resume."
)


async def _key_enables_withdrawals(client: ccxt_async.Exchange) -> bool:
    """STRICT, read-only. The API KEY's own "Enable Withdrawals" restriction,
    from Binance's key-permission endpoint (GET /sapi/v1/account/apiRestrictions,
    `enableWithdrawals`; weight 1, needs only Enable Reading). The account-level
    `canWithdraw` in the balance payload is NOT this: Binance reports it True
    for ordinary accounts whatever the key's restrictions are, so it is never
    consulted. Anything other than an explicit boolean (a failed call, a
    non-object response, a missing or non-boolean field) is "cannot verify":
    the ccxt error propagates, or WithdrawalPermissionUnverifiable is raised."""
    restrictions = await client.sapiGetAccountApiRestrictions()
    flag = restrictions.get("enableWithdrawals") if isinstance(restrictions, dict) else None
    if isinstance(flag, bool):
        return flag
    raise WithdrawalPermissionUnverifiable("Binance did not state whether withdrawals are enabled for this API key")


async def has_withdrawal_permission(client: ccxt_async.Exchange) -> bool:
    """Read-only. True only when Binance says THIS API KEY has withdrawals
    enabled (`enableWithdrawals: true`).

    Two DIFFERENT questions are asked of this answer, and they must never be
    confused:

    * ENTRY-CAPABLE validation (`assert_trade_only`, run by the new-entry
      path on every entry): any withdrawal permission -> refuse, and a key
      that cannot be verified -> refuse too (strict, see
      `_key_enables_withdrawals`).
    * RISK-REDUCING management of an ALREADY-OPEN position (reconcile,
      verify/re-place the bot's own protective order, the exit lease,
      emergency close): allowed with such a key, because those actions only
      reduce exposure and refusing them would strand the customer's
      position. That path runs on a Venue with `entries_allowed=False`,
      which cannot place an entry at all (venue.EntryForbidden).

    This function is the cycle-level / management-path read used for
    flagging the key. It is deliberately LENIENT: if the permission cannot be
    read it answers False ("not detected this cycle") and logs the failure
    category, so a hiccup in this one endpoint can never stall the management
    of an open position or set the durable block. That cannot let a trade
    through: every new entry re-validates strictly in `assert_trade_only`."""
    try:
        return await _key_enables_withdrawals(client)
    except Exception as exc:
        logger.warning("withdrawal-permission check unavailable this cycle (%s)", type(exc).__name__)
        return False


async def assert_trade_only(client: ccxt_async.Exchange) -> None:
    """ENTRY-CAPABLE credential validation: raises WithdrawalPermissionDetected
    if the key can withdraw, and fails closed (WithdrawalPermissionUnverifiable
    or the underlying Binance/network error) if that cannot be established.
    Called by the new-entry path every time, never skipped, never relaxed for
    managing an existing position (that path doesn't need it - it can only
    reduce exposure; see has_withdrawal_permission)."""
    if await _key_enables_withdrawals(client):
        raise WithdrawalPermissionDetected(
            "stored API key has withdrawal permission enabled - revoke it and issue a trade-only key"
        )


class UnsupportedAccountMode(RuntimeError):
    """The Binance account is in a configuration whose order and protective
    -stop semantics this bot does not (and must not guess to) handle:
    Hedge Mode, Multi-Assets Mode, Portfolio Margin, or a mode that could
    not be read at all. A new live entry is refused; nothing is ever
    switched on the customer's behalf."""


class UnsupportedSymbol(RuntimeError):
    """The pair is not available as an active market of the right kind on
    this exchange/environment, so no order is placed for it."""


def quote_currency(pair: str) -> str:
    return pair.split("/", 1)[1]


def exchange_symbol(pair: str, market_type: str) -> str:
    """ccxt's unified symbol for `pair` on `market_type`.

    The platform stores pairs as spot-style "BTC/USDT". On the Spot client
    that IS the ccxt symbol. On the USD-M client ccxt's symbol for the
    perpetual is "BTC/USDT:USDT" (settle currency after the colon); a bare
    "BTC/USDT" is neither a ccxt symbol nor a Binance market id there
    (BTCUSDT), so passing it through raises BadSymbol - this is the
    normalization the Futures path always needed. Pure and deterministic;
    the live path additionally verifies the result against loaded markets
    (`verify_market`)."""
    if market_type == "futures":
        return f"{pair}:{quote_currency(pair)}"
    return pair


def verify_market(client: ccxt_async.Exchange, symbol: str, *, market_type: str) -> dict:
    """The loaded market for `symbol`, or UnsupportedSymbol. Requires
    `client.load_markets()` to have run. For Futures it must be an active
    LINEAR perpetual; for Spot an active spot market."""
    markets = getattr(client, "markets", None) or {}
    market = markets.get(symbol)
    if not isinstance(market, dict):
        raise UnsupportedSymbol(f"{symbol} is not a market on this exchange")
    if market.get("active") is False:
        raise UnsupportedSymbol(f"{symbol} is not currently active")
    if market_type == "futures":
        if not (market.get("swap") is True and market.get("linear") is True and market.get("contract") is True):
            raise UnsupportedSymbol(f"{symbol} is not a linear USD-M perpetual")
    else:
        if market.get("spot") is not True:
            raise UnsupportedSymbol(f"{symbol} is not a spot market")
    return market


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


# Successful mode verifications, keyed by a digest of the API key (never the
# key itself), so MANAGING an open position (every 5 min, plus the 15 s
# recovery loop) doesn't spend Binance request weight re-reading two slow
# account settings each time. A NEW entry never uses it (max_age_seconds=0),
# and a failed verification is never cached.
_MODE_VERIFIED_AT: dict[str, float] = {}


def _mode_cache_key(client) -> str | None:
    api_key = getattr(client, "apiKey", None)
    if not api_key:
        return None
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()


async def verify_futures_account_mode(client: ccxt_async.Exchange, symbol: str, *, max_age_seconds: float = 0) -> None:
    """Fail closed unless the account is positively confirmed to be in the
    one configuration this bot supports: USD-M, ONE-WAY position mode,
    single-asset margin, NOT Portfolio Margin.

    "Positively confirmed" matters: every read below must succeed and
    return the safe value. A failed read, a missing field or an
    unexpected shape raises UnsupportedAccountMode, never a guess. This is
    read-only; no mode is ever changed.

    Known limit (see ARCHITECTURE.md): Portfolio Margin has no dedicated
    "is this PM" read on the classic Futures API. PM accounts cannot use
    the classic endpoints above (they must use `papi`), so a PM account is
    expected to fail the reads here and be refused; the extra SAPI probe
    only adds a positive detection where the key may call it. Whether
    Binance behaves exactly that way must be confirmed by the Testnet/
    Mainnet read-only validation before customers trade."""
    key = _mode_cache_key(client)
    if key is not None and max_age_seconds > 0:
        verified_at = _MODE_VERIFIED_AT.get(key)
        if verified_at is not None and time.monotonic() - verified_at < max_age_seconds:
            return
    try:
        mode = await client.fetch_position_mode(symbol)
    except Exception as exc:
        raise UnsupportedAccountMode("could not read the Binance Futures position mode") from exc
    hedged = mode.get("hedged") if isinstance(mode, dict) else None
    if hedged is not False:
        raise UnsupportedAccountMode("Binance Futures Hedge Mode is not supported" if hedged else "position mode unreadable")

    try:
        multi = await client.fapiPrivateGetMultiAssetsMargin()
    except Exception as exc:
        raise UnsupportedAccountMode("could not read the Binance Futures Multi-Assets Mode setting") from exc
    multi_value = _as_bool(multi.get("multiAssetsMargin")) if isinstance(multi, dict) else None
    if multi_value is not False:
        raise UnsupportedAccountMode(
            "Binance Futures Multi-Assets Mode is not supported" if multi_value else "multi-assets mode unreadable"
        )

    options = getattr(client, "options", {}) or {}
    if options.get("portfolioMargin") or options.get("papi"):
        raise UnsupportedAccountMode("Portfolio Margin routing is not supported")
    probe = getattr(client, "sapiGetPortfolioAccount", None)
    if probe is not None:
        try:
            result = await probe()
        except Exception:
            result = None
        if isinstance(result, dict) and result:
            raise UnsupportedAccountMode("Binance Portfolio Margin accounts are not supported")
    if key is not None:
        _MODE_VERIFIED_AT[key] = time.monotonic()


def safe_error_message(exc: Exception) -> str:
    """Turns any exception from a trading cycle into a short, generic,
    customer-facing message. Deliberately never includes `str(exc)` -
    ccxt/exchange error text can echo back request details (key
    fragments, raw HTTP bodies, internal URLs) that have no business
    reaching a customer's dashboard. Only the category is shown; the
    real exception and its traceback still go to the server log via
    `logger.exception` in scheduler.py, unaffected by this function.
    """
    if isinstance(exc, NonProductionRoutingError):
        return "The practice environment is not available right now. Nothing was traded."
    if isinstance(exc, UnsupportedAccountMode):
        return (
            "Your Binance Futures account is set to a mode this bot does not support (it needs standard "
            "One-way Mode with single-asset margin, not Hedge Mode, Multi-Assets Mode or Portfolio Margin). "
            "No new trade was opened."
        )
    if isinstance(exc, UnsupportedSymbol):
        return "This pair is not currently available to trade on Binance for your account type. No new trade was opened."
    if isinstance(exc, WithdrawalPermissionDetected):
        return (
            "Your Binance API key has withdrawal permission enabled. For your safety, trading is "
            "paused on this account until you replace it with a trade-only key."
        )
    if isinstance(exc, ccxt_async.AuthenticationError):
        return "Binance rejected this account's API key or secret. Please check it in your Binance account."
    if isinstance(exc, ccxt_async.InsufficientFunds):
        return "Your Binance balance is too low for the bot's current position size."
    if isinstance(exc, WithdrawalPermissionUnverifiable):
        return (
            "Could not verify your Binance API key's permissions just now, so no new trade was opened. "
            "This will retry automatically on the next check."
        )
    if isinstance(exc, (ccxt_async.NetworkError, ccxt_async.ExchangeNotAvailable, ccxt_async.RequestTimeout)):
        return "Temporary connection issue reaching Binance. The bot will try again on its next check."
    if isinstance(exc, ccxt_async.ExchangeError):
        return "Binance rejected a request from the bot. Please check your account settings on Binance."
    return "An unexpected error occurred while checking the market for this account."


async def fetch_symbol_leverage_and_margin_mode(
    client: ccxt_async.Exchange, pair: str, *, side: str
) -> tuple[float | None, str | None]:
    """Read-only. Returns the account's currently configured leverage for
    `pair`/`side` ("long" or "short"), or None if it can't be determined.

    This module never calls `set_leverage` - reading ccxt's own source
    for `binanceusdm.set_leverage` shows it is a mutating call with real,
    account-wide, cross-position side effects (it can change margin mode
    and liquidation prices for existing positions), which a trading bot
    must never do silently on a customer's behalf. `fetch_leverage`/
    `fetch_leverages`, by contrast, are plain read-only GETs (they back
    onto Binance's symbol-config/account-info endpoints) - safe to call
    every cycle purely to learn what leverage is already configured.

    Returns (leverage, margin_mode) from the one read. margin_mode is "isolated", "cross" or
    None (not reported); the pre-entry protection check refuses an entry when it is None (it is never assumed).

    A None return means the caller's margin ceiling (risk.
    apply_margin_ceiling) is simply skipped for this entry - never
    treated as "assume some leverage" or "assume no leverage". The
    independent notional ceiling (risk.apply_exposure_ceiling) still
    applies regardless of this result, so this is never the only safety
    net standing between a tight stop and an oversized position.
    """
    try:
        leverage = await client.fetch_leverage(pair)
    except Exception:
        return None, None
    if not isinstance(leverage, dict):
        return None, None
    mode = leverage.get("marginMode")
    mode = mode if mode in ("isolated", "cross") else None
    value = leverage.get("longLeverage") if side == "long" else leverage.get("shortLeverage")
    # Not a leverage: booleans, non-numbers, non-finite or non-positive values. The plausible RANGE (1..125) is NOT
    # applied here, so with the guards off this read behaves as it did before they existed; the pre-check applies it.
    if isinstance(value, str):  # some payloads carry numbers as strings
        try:
            value = float(value)
        except ValueError:
            return None, mode
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, mode
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return None, mode
    return value, mode


async def fetch_symbol_leverage(client: ccxt_async.Exchange, pair: str, *, side: str) -> float | None:
    """Leverage only; see fetch_symbol_leverage_and_margin_mode (same single read-only call)."""
    return (await fetch_symbol_leverage_and_margin_mode(client, pair, side=side))[0]


async def fetch_maintenance_tiers(client: ccxt_async.Exchange, pair: str) -> list[MaintenanceTier] | None:
    """Read-only. The symbol's Binance leverage brackets (notional range, maintenance-margin rate, maintenance
    amount `cum`), fetched fresh on every call (never cached here, so never stale by more than this call), or None
    when they cannot be read or are incomplete. The caller treats None as "cannot prove the stop is safe"."""
    try:
        params = {}
        try:
            params = {"symbol": client.market(pair)["id"]}  # one symbol instead of the whole exchange
        except Exception:  # noqa: BLE001
            params = {}
        response = await client.fetch_leverage_tiers([pair], params)
        raw = response.get(pair) if isinstance(response, dict) else None
        if not isinstance(raw, list) or not raw:
            return None
        out: list[MaintenanceTier] = []
        for item in raw:
            info = item.get("info") if isinstance(item, dict) else None
            if not isinstance(info, dict) or info.get("cum") is None:
                return None  # without the maintenance amount the rate alone would be wrong
            out.append(
                MaintenanceTier(
                    tier=item.get("tier"),
                    floor=float(item["minNotional"]),
                    cap=float(item["maxNotional"]),
                    rate=float(item["maintenanceMarginRate"]),
                    cum=float(info["cum"]),
                )
            )
        return out
    except Exception:  # noqa: BLE001
        return None


async def fetch_open_position_count(client: ccxt_async.Exchange) -> int | None:
    """Read-only. How many positions the whole account holds right now (any symbol), or None when the answer is
    not reliable (the call failed, or any entry could not be read). Used only to decide whether a cross-margin
    liquidation estimate for ONE position is defensible: other positions share the same collateral."""
    try:
        positions = await client.fetch_positions()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(positions, list):
        return None
    count = 0
    for pos in positions:
        if not isinstance(pos, dict):
            return None
        raw = pos.get("contracts")
        if raw is None and isinstance(pos.get("info"), dict):
            raw = pos["info"].get("positionAmt")
        if raw is None:
            return None
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(amount):
            return None
        if amount != 0:
            count += 1
    return count


async def fetch_available_quote_balance(client: ccxt_async.Exchange, pair: str) -> float:
    balance = await client.fetch_balance()
    quote = quote_currency(pair)
    entry = balance.get(quote)
    if isinstance(entry, dict) and entry.get("free") is not None:
        return float(entry["free"])
    free_map = balance.get("free", {})
    if isinstance(free_map, dict):
        return float(free_map.get(quote, 0.0) or 0.0)
    return 0.0
