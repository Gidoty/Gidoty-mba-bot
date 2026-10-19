"""Secrets must never reach ordinary logs, propagated exceptions or
customer-facing text.

Synthetic sentinels only. The Telegram bot token is in the request URL of
every Bot API call, so (a) any exception that carries a URL, (b) httpx's own
INFO request log and (c) an exception chain/context that still references the
original httpx error would each leak it. These tests force each of those."""
from __future__ import annotations

import asyncio
import http.server
import io
import logging
import threading
import traceback
from types import SimpleNamespace

import ccxt.async_support as ccxt
import httpx
import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot import log_safety, scheduler
from mba_bot.api.routers import billing, telegram as telegram_router
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import TradingAccount
from mba_bot.notifications import telegram as tg
from mba_bot.notifications.telegram import NotificationError, TelegramNotifier

TOKEN = "123456:SECRET-TELEGRAM-TOKEN-DO-NOT-LOG"
LEAKS = (TOKEN, "SECRET-TELEGRAM-TOKEN-DO-NOT-LOG", "/bot123456:")
FORBIDDEN = (*LEAKS, "api.telegram.org/bot")  # a full token-bearing URL; redaction keeps "bot<redacted>"


def _assert_clean(text: str, extra=(), *, redacted_form_ok=False):
    for needle in (*(LEAKS if redacted_form_ok else FORBIDDEN), *extra):
        assert needle not in text, f"leaked {needle!r}"


def _chain_text(exc: BaseException) -> str:
    parts, seen, stack = [], set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        parts += [str(e), repr(e), repr(e.args), "".join(traceback.format_exception(e))]
        stack += [e.__cause__, e.__context__]
    return "\n".join(parts)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _failing_transport(monkeypatch, handler):
    monkeypatch.setattr(
        tg.httpx, "AsyncClient", lambda **k: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **k)
    )


def _url_bearing(exc_cls):
    def handler(request: httpx.Request):
        raise exc_cls(f"failed calling {request.url}", request=request)

    return handler


SCENARIOS = {
    "http400": (lambda r: httpx.Response(400, json={"ok": False, "description": "Bad Request"}), "http", 400),
    "http403": (lambda r: httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"}), "http", 403),
    "http429": (lambda r: httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 5}}), "http", 429),
    "http500": (lambda r: httpx.Response(500, text="boom"), "http", 500),
    "connect": (_url_bearing(httpx.ConnectError), "network", None),
    "timeout": (_url_bearing(httpx.ReadTimeout), "timeout", None),
    "connect_timeout": (_url_bearing(httpx.ConnectTimeout), "timeout", None),
    "unexpected": (lambda r: (_ for _ in ()).throw(RuntimeError(f"weird {r.url}")), "unexpected", None),
}


# =========================== TelegramNotifier boundary ===========================


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_notifier_failures_carry_only_safe_information_and_no_chain(monkeypatch, name):
    handler, category, status = SCENARIOS[name]
    _failing_transport(monkeypatch, handler)

    with pytest.raises(NotificationError) as info:
        asyncio.run(TelegramNotifier(TOKEN).send("42", "hello"))

    exc = info.value
    _assert_clean(_chain_text(exc))
    assert exc.__cause__ is None and exc.__context__ is None, "no path back to the token-bearing httpx error"
    assert exc.category == category and exc.status_code == status
    assert str(exc) == (f"Telegram send failed: HTTP {status}" if status else f"Telegram send failed: {category}")


def test_status_helpers_distinguish_useful_categories(monkeypatch):
    _failing_transport(monkeypatch, SCENARIOS["http429"][0])
    with pytest.raises(NotificationError) as e429:
        asyncio.run(TelegramNotifier(TOKEN).send("1", "x"))
    _failing_transport(monkeypatch, SCENARIOS["http403"][0])
    with pytest.raises(NotificationError) as e403:
        asyncio.run(TelegramNotifier(TOKEN).send("1", "x"))
    assert e429.value.is_rate_limited and not e429.value.is_blocked_or_unauthorized
    assert e403.value.is_blocked_or_unauthorized and not e403.value.is_rate_limited


def test_the_sanitized_error_is_clean_even_when_send_runs_inside_a_callers_except_block(monkeypatch):
    _failing_transport(monkeypatch, SCENARIOS["connect"][0])

    async def caller():
        try:
            raise ValueError("caller's own, unrelated error")
        except ValueError:
            await TelegramNotifier(TOKEN).send("1", "x")  # NotificationError raised while handling ValueError

    with pytest.raises(NotificationError) as info:
        asyncio.run(caller())
    _assert_clean(_chain_text(info.value))  # the only context is the caller's unrelated error


def test_a_successful_send_logs_nothing_with_the_token_even_at_debug(monkeypatch, caplog):
    _failing_transport(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    caplog.set_level(logging.DEBUG)
    asyncio.run(TelegramNotifier(TOKEN).send("1", "x"))
    _assert_clean(caplog.text)
    assert logging.getLogger("httpx").level >= logging.WARNING, "httpx's INFO request log (full URL) stays off"


def test_notifier_repr_does_not_contain_the_token():
    _assert_clean(repr(TelegramNotifier(TOKEN)) + str(TelegramNotifier(TOKEN)))


def test_settings_repr_never_prints_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("JWT_SECRET", "JWT-SECRET-SENTINEL-1234")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "PAYSTACK-SECRET-SENTINEL-1234")
    monkeypatch.setenv("ENCRYPTION_KEY", "ENCRYPTION-KEY-SENTINEL-1234")
    monkeypatch.setenv("TESTNET_SPOT_API_SECRET", "TESTNET-SECRET-SENTINEL-1234")
    text = repr(load_settings())
    for needle in ("SECRET-TELEGRAM", "SENTINEL"):
        assert needle not in text


# =========================== real callers ===========================


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _live_settings(**kw):
    return make_settings(max_notional_multiplier=100.0, telegram_bot_token=TOKEN, **kw)


@pytest.mark.parametrize("name", ["http403", "http429", "connect", "timeout", "http400"])
def test_a_live_trade_opens_and_is_protected_while_telegram_fails_and_logs_stay_clean(db, monkeypatch, caplog, name):
    """Real path: scheduler._run_one -> process_account -> lifecycle._notify, with the
    real TelegramNotifier failing. Safety work is unaffected; nothing leaks."""
    _failing_transport(monkeypatch, SCENARIOS[name][0])
    settings = _live_settings()
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)
    caplog.set_level(logging.DEBUG)

    asyncio.run(scheduler._run_one(account_id, settings))

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected", "Telegram failure must not interrupt protection"
    _assert_clean(caplog.text)
    handler, category, status = SCENARIOS[name]
    assert "Telegram send failed" in caplog.text, "the failure is still visible, in a safe form"
    if status:
        assert f"HTTP {status}" in caplog.text


def test_the_withdrawal_alert_failing_to_send_is_logged_safely_and_the_block_still_holds(db, monkeypatch, caplog):
    _failing_transport(monkeypatch, SCENARIOS["http403"][0])
    settings = _live_settings()
    client = spot_client(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings)
    caplog.set_level(logging.DEBUG)

    asyncio.run(scheduler._run_one(account_id, settings))

    with session_scope() as session:
        assert session.get(TradingAccount, account_id).withdrawal_blocked_at is not None
    assert client.orders == {}
    _assert_clean(caplog.text)
    assert "HTTP 403" in caplog.text


def test_an_unwrapped_notifier_failure_in_a_paper_cycle_is_logged_safely_by_the_scheduler(db, monkeypatch, caplog):
    """Paper entries call notifier.send directly (no wrapper): the exception reaches
    scheduler._run_one's logger.exception - which must still be clean."""
    _failing_transport(monkeypatch, SCENARIOS["http403"][0])
    settings = _live_settings(testnet_spot_api_key="testnet-key-1234567890", testnet_spot_api_secret="testnet-secret-1234567890")
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)
    with session_scope() as session:
        session.get(TradingAccount, account_id).bot_started_at = None  # PAPER mode
    caplog.set_level(logging.DEBUG)

    asyncio.run(scheduler._run_one(account_id, settings))

    _assert_clean(caplog.text)
    assert "Telegram send failed: HTTP 403" in caplog.text


def test_the_telegram_webhook_reply_failure_is_logged_safely(monkeypatch, caplog):
    _failing_transport(monkeypatch, SCENARIOS["http403"][0])
    caplog.set_level(logging.DEBUG)
    settings = SimpleNamespace(telegram_bot_token=TOKEN)
    asyncio.run(telegram_router._reply("555", "hi", settings=settings))
    _assert_clean(caplog.text)
    assert "HTTP 403" in caplog.text


# =========================== defense in depth: redaction ===========================


def test_redaction_replaces_configured_secrets_and_any_bot_url_token():
    out = log_safety.redact(
        f"x https://api.telegram.org/bot{TOKEN}/sendMessage y JWT-SECRET-SENTINEL-1234 z other bot999:UNKNOWN-TOKEN-FORM_x",
        ["JWT-SECRET-SENTINEL-1234", "short"],
    )
    assert "JWT-SECRET" not in out and "SECRET-TELEGRAM" not in out and "UNKNOWN-TOKEN" not in out
    assert log_safety.redact("a short word", ["short"]) == "a short word"


def _handler_on_root():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    return stream, handler, root


def test_installed_filter_redacts_messages_and_tracebacks_even_if_something_logs_a_token():
    stream, handler, root = _handler_on_root()
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        settings = SimpleNamespace(
            telegram_bot_token=TOKEN, whatsapp_provider_token=None, paystack_secret_key="PAYSTACK-SECRET-SENTINEL-1234",
            jwt_secret="JWT-SECRET-SENTINEL-1234", encryption_key=None, testnet_spot_api_key=None, testnet_spot_api_secret=None,
            testnet_futures_api_key=None, testnet_futures_api_secret=None,
        )
        log_safety.install_log_safety(settings)
        log = logging.getLogger("some.module")
        log.info("sending to https://api.telegram.org/bot%s/sendMessage", TOKEN)
        try:
            raise RuntimeError(f"upstream said JWT-SECRET-SENTINEL-1234 and PAYSTACK-SECRET-SENTINEL-1234 at {TOKEN}")
        except RuntimeError:
            log.exception("boom")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    text = stream.getvalue()
    _assert_clean(text, extra=("JWT-SECRET-SENTINEL", "PAYSTACK-SECRET-SENTINEL"), redacted_form_ok=True)
    assert "sending to" in text and "boom" in text and "RuntimeError" in text, "logs stay useful"


def test_even_if_httpx_request_logging_were_re_enabled_the_token_is_redacted():
    stream, handler, root = _handler_on_root()
    old_root, old_httpx = root.level, logging.getLogger("httpx").level
    root.setLevel(logging.INFO)
    try:
        log_safety.install_log_safety(SimpleNamespace(telegram_bot_token=TOKEN))
        logging.getLogger("httpx").setLevel(logging.INFO)  # someone turns request logging back on
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        real = httpx.AsyncClient
        tg.httpx.AsyncClient = lambda **k: real(transport=transport, **k)
        try:
            asyncio.run(TelegramNotifier(TOKEN).send("1", "x"))
        finally:
            tg.httpx.AsyncClient = real
    finally:
        root.removeHandler(handler)
        root.setLevel(old_root)
        logging.getLogger("httpx").setLevel(old_httpx)
    text = stream.getvalue()
    assert "HTTP Request" in text, "precondition: the library really did log the request"
    _assert_clean(text, redacted_form_ok=True)


# =========================== broader review: Binance and Paystack ===========================

API_KEY = "BINANCE-API-KEY-SENTINEL-abcdef123456"
API_SECRET = "BINANCE-API-SECRET-SENTINEL-abcdef123456"


class _Binance401(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


def _ccxt_client(base_url: str):
    client = ccxt.binance({"apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": False})
    for key, value in list(client.urls["api"].items()):
        if isinstance(value, str):
            client.urls["api"][key] = base_url
    return client


def _fail_with_ccxt(base_url: str):
    async def go():
        client = _ccxt_client(base_url)
        try:
            await client.privateGetAccount()
        finally:
            await client.close()

    try:
        asyncio.run(go())
    except Exception as exc:
        return exc
    raise AssertionError("expected a ccxt failure")


def _no_binance_secret(text: str):
    for needle in (API_KEY, API_SECRET):
        assert needle not in text


def test_ccxt_authentication_error_text_and_traceback_carry_no_api_key_or_secret(caplog):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Binance401)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        caplog.set_level(logging.INFO)  # the production level (main.configure_logging)
        exc = _fail_with_ccxt(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert isinstance(exc, ccxt.AuthenticationError)
    _no_binance_secret(_chain_text(exc) + caplog.text)
    # and the customer-facing text is the sanitized category, not the exchange body
    from mba_bot import exchange

    assert "-2015" not in exchange.safe_error_message(exc) and "Invalid API-key" not in exchange.safe_error_message(exc)


def test_ccxt_network_failure_text_and_traceback_carry_no_api_key_or_secret(caplog):
    caplog.set_level(logging.INFO)  # the production level (main.configure_logging)
    exc = _fail_with_ccxt("http://127.0.0.1:9")  # nothing listens here
    assert isinstance(exc, ccxt.NetworkError)
    _no_binance_secret(_chain_text(exc) + caplog.text)


def test_paystack_failure_logs_do_not_contain_the_secret_key(caplog, monkeypatch):
    secret = "PAYSTACK-SECRET-KEY-SENTINEL-abcdef123456"
    request = httpx.Request("POST", "https://api.paystack.co/transaction/initialize")
    monkeypatch.setattr(
        billing.httpx, "post",
        lambda *a, **k: httpx.Response(401, json={"status": False, "message": "Invalid key"}, request=request),
    )
    settings = SimpleNamespace(paystack_secret_key=secret, paystack_amount=1000, paystack_currency="NGN", frontend_origin="http://x")
    customer = SimpleNamespace(has_paid=False, email="a@example.com", id="c1")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(Exception):
        billing.initialize_payment(customer=customer, settings=settings)
    assert secret not in caplog.text
