"""The demo runner (docs/safety/demo_runner.md). Ships behind DEMO_RUNNER (default off).

DEMO mode runs the full live path (real orders, protective stop, reconciliation) against Binance Futures Demo Trading or Spot Testnet with the customer's OWN demo keys, kept in their own table
(`demo_credentials`), labelled DEMO everywhere and kept apart from live and paper records (`trades.is_demo`). It can never use real keys by mistake:

  * demo keys live in a separate table and are only ever read in DEMO mode; live keys (`api_credentials`, tagged mainnet) are never read in DEMO mode;
  * a DEMO client is always built with sandbox=True, and `exchange.build_exchange_client` proves from the effective URL table that it routes only to Demo Trading / Spot Testnet hosts
    (`exchange.assert_routing`); a live client is always sandbox=False and is proven to route to production only;
  * entering demo keys and switching DEMO on both make one authenticated read against the demo endpoint. A key Binance does not accept there (a real key, a key for the other system) is refused
    and nothing is switched on; a key equal to the customer's stored live key is refused without any call;
  * the account must not be live (live trading enabled or the bot started) to be in DEMO, and live cannot be enabled while DEMO is on.

Pure helpers and fixed texts only; the API routes and the trader call them."""
from __future__ import annotations

from .crypto_utils import decrypt

TAG = "DEMO"

MSG_OFF = "Demo mode is not available on this service."
MSG_KEYS_REFUSED = (
    "Binance did not accept these keys on the demo system. Use keys created for Binance Futures Demo Trading (for a Futures account) or the Spot Testnet (for a Spot account). "
    "Real keys do not work there and are never accepted here."
)
MSG_SAME_AS_LIVE = "These look like your live Binance keys. Demo mode only accepts keys created on the demo system. Your live keys were not touched."
MSG_NEED_KEYS = "Enter your demo keys before switching demo mode on."
MSG_LIVE_ACTIVE = "Demo mode cannot be on while live trading is enabled for this account."
MSG_LIVE_BLOCKED = "Turn demo mode off before enabling live trading."
MSG_DEMO_LABEL = "DEMO: practice orders on Binance's demo system. No real money."


def available(account, settings) -> bool:
    """True when this account's cycle must run in DEMO mode: the switch is on, the customer switched demo mode on, demo keys are on file, and the account is not live-enabled."""
    return bool(
        settings.demo_runner
        and account.demo_enabled_at is not None
        and getattr(account, "demo_credential", None) is not None
        and account.live_trading_enabled_at is None
        and account.bot_started_at is None
    )


def credentials(account, settings) -> tuple[str | None, str | None]:
    """The decrypted demo key and secret, or (None, None). Never reads the live credential."""
    cred = getattr(account, "demo_credential", None)
    if cred is None or not settings.encryption_key:
        return None, None
    return decrypt(cred.encrypted_api_key, key=settings.encryption_key), decrypt(cred.encrypted_api_secret, key=settings.encryption_key)


def label(is_paper: bool, is_demo: bool) -> str:
    return TAG if is_demo else ("PAPER" if is_paper else "LIVE")
