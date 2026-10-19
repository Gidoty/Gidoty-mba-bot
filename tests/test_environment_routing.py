"""Environment routing: which Binance host each kind of client can reach.

Runs the REAL ccxt 4.5.88 classes with placeholder credentials and no network
(requests are built by ccxt's own `sign()`, which is exactly what the library
sends). Proves routing, not Binance behaviour: that Futures Demo Trading works
end to end is validated separately from the VPS (ARCHITECTURE.md).

  sandbox=False                -> production hosts only
  sandbox=True + spot          -> Spot Testnet (testnet.binance.vision), unchanged
  sandbox=True + futures       -> Binance Futures DEMO (demo-*.binance.com) only;
                                  never production, never the legacy Futures Testnet
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import ccxt.async_support as ccxt_async
import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot import config, exchange, scheduler, trader
from mba_bot.db import init_db, session_scope
from mba_bot.exchange import FUTURES_DEMO_HOSTS, NonProductionRoutingError, build_exchange_client
from mba_bot.models import TradingAccount

PRODUCTION_HOSTS = {"api.binance.com", "fapi.binance.com", "dapi.binance.com", "eapi.binance.com", "papi.binance.com"}
LEGACY_FUTURES_TESTNET = "testnet.binancefuture.com"
KEY, SECRET = "placeholder-api-key", "placeholder-api-secret"  # not credentials


def _client(market_type: str, sandbox: bool):
    return build_exchange_client(exchange="binance", market_type=market_type, api_key=KEY, api_secret=SECRET, sandbox=sandbox)


def _table_hosts(client) -> set[str]:
    return {urlparse(v).netloc for v in client.urls["api"].values() if isinstance(v, str)}


def _request_host_or_refusal(client, group: str, path: str):
    """The host ccxt would send this request to, or the NotSupported ccxt raises instead."""
    try:
        signed = client.sign(path, group, "GET", {})
    except ccxt_async.NotSupported:
        return "REFUSED"
    return urlparse(signed["url"]).netloc


def _every_endpoint_group(client):
    """(group, an example path) for EVERY endpoint group ccxt can send a request to."""
    for group, methods in client.api.items():
        for verb, paths in methods.items():
            example = next(iter(paths if isinstance(paths, (list, dict)) else []), None)
            if example is not None:
                yield group, example
                break


# =========================== Futures Demo: sandbox=True + futures ===========================


def test_futures_paper_client_is_in_demo_trading_mode_not_sandbox_mode():
    client = _client("futures", sandbox=True)
    assert client.options.get("enableDemoTrading") is True
    assert client.isSandboxModeEnabled is False, "the legacy Futures Testnet mode must not be active"
    assert type(client) is ccxt_async.binanceusdm


def test_futures_paper_table_contains_only_demo_hosts_and_never_production_or_legacy_testnet():
    hosts = _table_hosts(_client("futures", sandbox=True))
    assert hosts and hosts <= FUTURES_DEMO_HOSTS
    assert not (hosts & PRODUCTION_HOSTS), "a Futures paper client can route to a production host"
    assert LEGACY_FUTURES_TESTNET not in hosts and "testnet.binance.vision" not in hosts


def test_every_request_a_futures_paper_client_can_build_goes_to_demo_or_is_refused_never_to_production():
    """The proof, over EVERY endpoint group ccxt knows (not just the ones the bot calls today)."""
    client = _client("futures", sandbox=True)
    outcomes = {}
    for group, path in _every_endpoint_group(client):
        outcomes[group] = _request_host_or_refusal(client, group, path)
    assert outcomes, "precondition: endpoint groups were enumerated"
    for group, outcome in outcomes.items():
        assert outcome == "REFUSED" or outcome in FUTURES_DEMO_HOSTS, f"{group} -> {outcome}"
        assert outcome not in PRODUCTION_HOSTS and outcome != LEGACY_FUTURES_TESTNET, f"{group} -> {outcome}"
    # the groups the bot uses resolve to the demo Futures host
    for group, path in (("fapiPrivate", "order"), ("fapiPrivate", "algoOrder"), ("fapiPrivateV3", "positionRisk"),
                        ("fapiPrivate", "positionSide/dual"), ("fapiPublic", "exchangeInfo"), ("fapiPublic", "klines")):
        assert outcomes.get(group, _request_host_or_refusal(client, group, path)) == "demo-fapi.binance.com"
    # groups Demo does not offer are refused, not rerouted
    for group in ("sapi", "papi", "eapi"):
        if group in client.api:
            assert _request_host_or_refusal(client, group, "x") == "REFUSED", group


def test_the_legacy_futures_sandbox_path_is_never_taken(monkeypatch):
    calls = []
    real = ccxt_async.binanceusdm.set_sandbox_mode
    monkeypatch.setattr(ccxt_async.binanceusdm, "set_sandbox_mode", lambda self, enable: calls.append(enable) or real(self, enable))
    _client("futures", sandbox=True)
    assert calls == [], "Futures paper must not call set_sandbox_mode"


def test_a_signed_futures_paper_order_url_is_demo(monkeypatch):
    client = _client("futures", sandbox=True)
    signed = client.sign("order", "fapiPrivate", "POST", {"symbol": "BTCUSDT"})
    assert urlparse(signed["url"]).netloc == "demo-fapi.binance.com"


# =========================== fail closed ===========================


def test_futures_paper_fails_closed_if_demo_mode_cannot_be_enabled(monkeypatch):
    def refuse(self, enable):
        raise ccxt_async.NotSupported("demo trading unavailable")

    monkeypatch.setattr(ccxt_async.binanceusdm, "enable_demo_trading", refuse)
    with pytest.raises(NonProductionRoutingError):
        _client("futures", sandbox=True)


def test_futures_paper_fails_closed_if_demo_mode_silently_does_nothing(monkeypatch):
    monkeypatch.setattr(ccxt_async.binanceusdm, "enable_demo_trading", lambda self, enable: None)  # leaves PRODUCTION urls
    with pytest.raises(NonProductionRoutingError):
        _client("futures", sandbox=True)


def test_futures_paper_fails_closed_if_demo_leaves_a_production_endpoint_in_the_table(monkeypatch):
    real = ccxt_async.binanceusdm.enable_demo_trading

    def leaky(self, enable):
        real(self, enable)
        self.urls["api"] = {**self.urls["api"], "fapiPrivateV3": "https://fapi.binance.com/fapi/v3"}

    monkeypatch.setattr(ccxt_async.binanceusdm, "enable_demo_trading", leaky)
    with pytest.raises(NonProductionRoutingError):
        _client("futures", sandbox=True)


def test_futures_paper_fails_closed_if_it_ends_up_on_the_legacy_testnet(monkeypatch):
    real = ccxt_async.binanceusdm.set_sandbox_mode
    monkeypatch.setattr(ccxt_async.binanceusdm, "enable_demo_trading", lambda self, enable: real(self, True))
    with pytest.raises(NonProductionRoutingError):
        _client("futures", sandbox=True)


def test_routing_failure_never_falls_back_to_another_environment(monkeypatch):
    monkeypatch.setattr(ccxt_async.binanceusdm, "enable_demo_trading", lambda self, enable: None)
    built = []
    real_init = ccxt_async.binanceusdm.__init__
    monkeypatch.setattr(ccxt_async.binanceusdm, "__init__", lambda self, *a, **k: built.append(1) or real_init(self, *a, **k))
    with pytest.raises(NonProductionRoutingError):
        _client("futures", sandbox=True)
    assert len(built) == 1, "one attempt, no retry on another environment"


def test_a_production_client_found_routing_to_demo_or_testnet_is_refused():
    client = _client("futures", sandbox=False)
    client.urls["api"] = {**client.urls["api"], "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1"}
    with pytest.raises(NonProductionRoutingError):
        exchange.assert_routing(client, market_type="futures", sandbox=False)
    client2 = _client("spot", sandbox=False)
    client2.options["enableDemoTrading"] = True
    with pytest.raises(NonProductionRoutingError):
        exchange.assert_routing(client2, market_type="spot", sandbox=False)


def test_the_error_is_customer_safe():
    assert "Nothing was traded" in exchange.safe_error_message(NonProductionRoutingError("host xyz"))


# =========================== unchanged: production and Spot Testnet ===========================


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_production_clients_route_to_production_only(market):
    client = _client(market, sandbox=False)
    hosts = _table_hosts(client)
    assert hosts <= PRODUCTION_HOSTS and "fapi.binance.com" in hosts
    assert not any("demo" in h or "testnet" in h for h in hosts)
    assert client.isSandboxModeEnabled is False and not client.options.get("enableDemoTrading")
    assert _request_host_or_refusal(client, "fapiPrivate" if market == "futures" else "private", "order") in PRODUCTION_HOSTS


def test_spot_testnet_behaviour_is_unchanged(monkeypatch):
    demo_calls = []
    monkeypatch.setattr(ccxt_async.binance, "enable_demo_trading", lambda self, enable: demo_calls.append(enable))
    client = _client("spot", sandbox=True)
    assert demo_calls == [], "Spot never uses demo trading"
    assert client.isSandboxModeEnabled is True
    assert _request_host_or_refusal(client, "private", "order") == "testnet.binance.vision"
    assert _request_host_or_refusal(client, "public", "ticker/price") == "testnet.binance.vision"
    hosts = _table_hosts(client)
    assert not (hosts & PRODUCTION_HOSTS) and hosts <= {"testnet.binance.vision", LEGACY_FUTURES_TESTNET}
    assert _request_host_or_refusal(client, "sapi", "x") == "REFUSED"


# =========================== credentials: separate, and legacy never used ===========================


def test_settings_have_no_legacy_futures_testnet_fields():
    names = {f.name for f in config.Settings.__dataclass_fields__.values()}
    assert {"demo_futures_api_key", "demo_futures_api_secret", "testnet_spot_api_key", "testnet_spot_api_secret"} <= names
    assert not any("testnet_futures" in n for n in names)


def test_only_the_demo_variables_feed_futures_and_the_legacy_ones_are_ignored(monkeypatch):
    for name in ("TESTNET_FUTURES_API_KEY", "TESTNET_FUTURES_API_SECRET", "DEMO_FUTURES_API_KEY", "DEMO_FUTURES_API_SECRET",
                 "TESTNET_SPOT_API_KEY", "TESTNET_SPOT_API_SECRET", "TESTNET_API_KEY", "TESTNET_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TESTNET_FUTURES_API_KEY", "legacy-futures-key-sentinel")
    monkeypatch.setenv("TESTNET_FUTURES_API_SECRET", "legacy-futures-secret-sentinel")
    legacy_only = config.load_settings()
    assert legacy_only.demo_futures_api_key is None and legacy_only.demo_futures_api_secret is None
    assert "legacy-futures" not in repr(legacy_only)

    monkeypatch.setenv("DEMO_FUTURES_API_KEY", "demo-futures-key-sentinel")
    monkeypatch.setenv("DEMO_FUTURES_API_SECRET", "demo-futures-secret-sentinel")
    monkeypatch.setenv("TESTNET_SPOT_API_KEY", "spot-testnet-key-sentinel")
    monkeypatch.setenv("TESTNET_SPOT_API_SECRET", "spot-testnet-secret-sentinel")
    both = config.load_settings()
    assert (both.demo_futures_api_key, both.demo_futures_api_secret) == ("demo-futures-key-sentinel", "demo-futures-secret-sentinel")
    assert (both.testnet_spot_api_key, both.testnet_spot_api_secret) == ("spot-testnet-key-sentinel", "spot-testnet-secret-sentinel")


def test_a_legacy_variable_triggers_a_value_free_warning_only(monkeypatch, caplog):
    monkeypatch.setenv("TESTNET_FUTURES_API_KEY", "legacy-futures-key-sentinel")
    monkeypatch.delenv("TESTNET_FUTURES_API_SECRET", raising=False)
    caplog.set_level(logging.WARNING)
    assert config.warn_about_legacy_futures_testnet_env(logging.getLogger("t")) is True
    assert "TESTNET_FUTURES_API_KEY" in caplog.text and "IGNORED" in caplog.text
    assert "sentinel" not in caplog.text
    monkeypatch.delenv("TESTNET_FUTURES_API_KEY")
    assert config.warn_about_legacy_futures_testnet_env(logging.getLogger("t")) is False


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _paper_futures_account(settings):
    account_id = new_account(settings, market_type="futures")
    with session_scope() as session:
        session.get(TradingAccount, account_id).bot_started_at = None  # resolves to PAPER
    return account_id


def test_paper_futures_uses_demo_credentials_and_the_non_production_flag(db, monkeypatch):
    settings = make_settings(demo_futures_api_key="DEMO-KEY-PLACEHOLDER", demo_futures_api_secret="DEMO-SECRET-PLACEHOLDER",
                             testnet_spot_api_key="SPOT-KEY-PLACEHOLDER", testnet_spot_api_secret="SPOT-SECRET-PLACEHOLDER")
    seen = []
    client = futures_client()
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kw: seen.append(kw) or client)
    run_cycle(_paper_futures_account(settings), settings)
    assert seen and seen[0]["sandbox"] is True and seen[0]["market_type"] == "futures"
    assert (seen[0]["api_key"], seen[0]["api_secret"]) == ("DEMO-KEY-PLACEHOLDER", "DEMO-SECRET-PLACEHOLDER")


def test_paper_spot_keeps_using_the_spot_testnet_credentials(db, monkeypatch):
    settings = make_settings(demo_futures_api_key="DEMO-KEY-PLACEHOLDER", demo_futures_api_secret="DEMO-SECRET-PLACEHOLDER",
                             testnet_spot_api_key="SPOT-KEY-PLACEHOLDER", testnet_spot_api_secret="SPOT-SECRET-PLACEHOLDER")
    seen = []
    client = spot_client()
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kw: seen.append(kw) or client)
    account_id = new_account(settings)
    with session_scope() as session:
        session.get(TradingAccount, account_id).bot_started_at = None
    run_cycle(account_id, settings)
    assert (seen[0]["api_key"], seen[0]["sandbox"]) == ("SPOT-KEY-PLACEHOLDER", True)


def test_without_demo_keys_a_futures_paper_account_builds_no_client_at_all(db, monkeypatch):
    settings = make_settings(demo_futures_api_key=None, demo_futures_api_secret=None)  # legacy keys, if any, were never loaded
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kw: pytest.fail("a client was built without Demo credentials"))
    run_cycle(_paper_futures_account(settings), settings, swallow=True)


def test_a_live_account_never_uses_the_demo_environment_or_demo_keys(db, monkeypatch):
    settings = make_settings(demo_futures_api_key="DEMO-KEY-PLACEHOLDER", demo_futures_api_secret="DEMO-SECRET-PLACEHOLDER")
    seen = []
    client = futures_client()
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kw: seen.append(kw) or client)
    account_id = new_account(settings, market_type="futures", email="liveroute@example.com")
    run_cycle(account_id, settings)
    assert seen and all(kw["sandbox"] is False for kw in seen)
    assert all(kw["api_key"] != "DEMO-KEY-PLACEHOLDER" for kw in seen)


def test_a_real_futures_paper_cycle_sends_its_first_request_to_binance_demo(db, monkeypatch):
    """End to end through trader.process_account with the REAL builder: the first request
    ccxt would put on the wire (load_markets) targets the demo host. No network is used."""
    settings = make_settings(demo_futures_api_key="DEMO-KEY-PLACEHOLDER", demo_futures_api_secret="DEMO-SECRET-PLACEHOLDER")
    urls = []

    async def record(self, url, method="GET", headers=None, body=None):
        urls.append(url)
        raise RuntimeError("stop: no network in tests")

    monkeypatch.setattr(ccxt_async.Exchange, "fetch", record)
    run_cycle(_paper_futures_account(settings), settings, swallow=True)
    assert urls, "precondition: a request was built"
    assert {urlparse(u).netloc for u in urls} == {"demo-fapi.binance.com"}


def test_credentials_test_endpoint_reports_a_routing_failure_safely_instead_of_crashing(monkeypatch):
    """POST /api/account/credentials/test builds a client from a customer-tagged key: a routing
    failure must come back as a sanitized 'not ok' result, never a 500 or a Mainnet fallback."""
    import asyncio as _asyncio
    from types import SimpleNamespace

    from mba_bot import crypto_utils
    from mba_bot.api.routers import account as account_router

    key = crypto_utils.generate_key()
    credential = SimpleNamespace(
        encrypted_api_key=crypto_utils.encrypt("k-placeholder", key=key),
        encrypted_api_secret=crypto_utils.encrypt("s-placeholder", key=key),
        environment="testnet",
    )
    customer = SimpleNamespace(account=SimpleNamespace(credential=credential, market_type="futures"))
    monkeypatch.setattr(account_router, "_require_account", lambda c: c.account)
    monkeypatch.setattr(ccxt_async.binanceusdm, "enable_demo_trading", lambda self, enable: None)  # routing cannot be established

    result = _asyncio.run(
        account_router.test_credentials.__wrapped__(customer=customer, db=None, settings=SimpleNamespace(encryption_key=key))
        if hasattr(account_router.test_credentials, "__wrapped__")
        else account_router.test_credentials(customer=customer, db=None, settings=SimpleNamespace(encryption_key=key))
    )
    assert result.ok is False and "Nothing was traded" in result.message
