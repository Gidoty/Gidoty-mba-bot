"""Log redaction must not break structured log records.

uvicorn's AccessFormatter unpacks `record.args` as
(client_addr, method, full_path, http_version, status_code). The first version
of the log-record redaction flattened every record to `msg=<formatted text>,
args=None`, so each access-log line failed with
`TypeError: cannot unpack non-iterable NoneType object`. Redaction has to keep
`args` structurally intact (same container type, same length, non-string
values untouched) while still removing secrets before any handler sees them."""
from __future__ import annotations

import io
import logging
import socket
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest
import uvicorn
from uvicorn.logging import AccessFormatter

from mba_bot import log_safety

TOKEN = "123456:SECRET-TELEGRAM-TOKEN-DO-NOT-LOG"
SETTINGS = SimpleNamespace(
    telegram_bot_token=TOKEN, whatsapp_provider_token=None, paystack_secret_key="PAYSTACK-SECRET-SENTINEL-abcdef123456",
    jwt_secret="JWT-SECRET-SENTINEL-abcdef123456", encryption_key=None, testnet_spot_api_key=None, testnet_spot_api_secret=None,
    demo_futures_api_key=None, demo_futures_api_secret=None,
)
ACCESS_FORMAT = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'


def _logger(name: str) -> logging.Logger:
    """A usable logger by name. Alembic's env.py calls logging.config.fileConfig (migration
    tests), which DISABLES every logger that already exists - including ones other test modules
    created earlier - so a reused name can be silently dead depending on test order."""
    logger = logging.getLogger(name)
    logger.disabled = False
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    return logger


@pytest.fixture
def safety():
    log_safety.install_log_safety(SETTINGS)
    yield
    log_safety.uninstall_log_safety()


@pytest.fixture
def access_logger():
    """A 'uvicorn.access' logger wired like uvicorn's own (AccessFormatter, no propagation),
    reporting any logging error instead of letting logging swallow it to stderr."""
    stream = io.StringIO()
    errors = []

    class Handler(logging.StreamHandler):
        def handleError(self, record):  # logging's hook for exceptions raised while formatting
            import sys

            errors.append(sys.exc_info()[1])

    handler = Handler(stream)
    handler.setFormatter(AccessFormatter(ACCESS_FORMAT, use_colors=False))
    logger = logging.getLogger("uvicorn.access")
    old = (logger.level, logger.propagate, list(logger.handlers))
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    yield logger, stream, errors
    logger.handlers, logger.propagate = old[2], old[1]
    logger.setLevel(old[0])


def test_the_reported_failure_a_plain_404_access_line_logs_normally_with_log_safety_installed(safety, access_logger):
    logger, stream, errors = access_logger
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:39734", "GET", "/", "1.0", 404)

    assert errors == [], f"AccessFormatter raised: {errors!r}"
    assert '127.0.0.1:39734 - "GET / HTTP/1.0" 404 Not Found' in stream.getvalue()


def test_the_record_keeps_its_structured_args(safety):
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    logger = logging.getLogger("uvicorn.access.structure")
    logger.addHandler(Capture())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/", "1.0", 404)

    (record,) = captured
    assert record.args == ("127.0.0.1:1", "GET", "/", "1.0", 404)
    assert isinstance(record.args, tuple) and isinstance(record.args[4], int), "type and length preserved"


def test_a_secret_inside_an_access_log_argument_is_redacted_and_the_structure_survives(safety, access_logger):
    logger, stream, errors = access_logger
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "POST", f"/api/hook/bot{TOKEN}/x?k=JWT-SECRET-SENTINEL-abcdef123456", "1.1", 200)

    assert errors == []
    out = stream.getvalue()
    assert "SECRET-TELEGRAM-TOKEN-DO-NOT-LOG" not in out and TOKEN not in out and "JWT-SECRET-SENTINEL" not in out
    assert '"POST /api/hook/bot<redacted>/x?k=<redacted> HTTP/1.1" 200 OK' in out


def test_plain_messages_and_dict_args_are_redacted_without_losing_their_shape(safety, caplog):
    caplog.set_level(logging.INFO)
    log = _logger("uvicorn_safety_test.plain")
    log.info("token is %s and count %d", TOKEN, 3)
    log.info("mapping %(who)s %(n)d", {"who": TOKEN, "n": 7})
    log.info(f"already formatted {TOKEN}")
    assert "SECRET-TELEGRAM" not in caplog.text
    assert "count 3" in caplog.text and "mapping <redacted> 7" in caplog.text
    structured = [r for r in caplog.records if r.getMessage().startswith("token is")][0]
    assert structured.args == ("<redacted>", 3)


def test_a_secret_split_across_template_and_argument_is_still_removed(safety, caplog):
    """'bot%s' + '123456:SECRET...' only matches the token pattern once formatted: this case
    (and any non-string argument whose str() carries a secret) falls back to a flattened,
    redacted record - secrecy over structure for our own free-form messages."""
    caplog.set_level(logging.INFO)

    class Carries:
        def __str__(self):
            return f"x bot{TOKEN} y"

    _logger("uvicorn_safety_test.split").info("url https://api.telegram.org/bot%s/sendMessage", TOKEN)
    _logger("uvicorn_safety_test.split").info("obj %s", Carries())
    assert "SECRET-TELEGRAM" not in caplog.text
    assert "https://api.telegram.org/bot<redacted>/sendMessage" in caplog.text


def test_tracebacks_are_still_redacted(safety, caplog):
    caplog.set_level(logging.INFO)
    try:
        raise RuntimeError(f"upstream said {TOKEN}")
    except RuntimeError:
        _logger("uvicorn_safety_test.tb").exception("boom %s", "ok")
    assert "SECRET-TELEGRAM" not in caplog.text and "boom ok" in caplog.text and "RuntimeError" in caplog.text


def test_a_real_uvicorn_server_logs_a_404_access_line_without_a_logging_error(safety, capsys):
    """The production scenario end to end: a real uvicorn server, its own AccessFormatter, a real
    HTTP GET / that returns 404, with log safety installed."""
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send({"type": "http.response.start", "status": 404, "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"access": {"()": "uvicorn.logging.AccessFormatter", "fmt": ACCESS_FORMAT, "use_colors": False}},
        "handlers": {"access": {"class": "logging.StreamHandler", "formatter": "access", "stream": "ext://sys.stdout"}},
        "loggers": {"uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False}},
    }
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=log_config, access_log=True, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        # uvicorn's config reset the logging record factory? make sure our safety is what is active
        log_safety.install_log_safety(SETTINGS)
        with pytest.raises(Exception) as http_error:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
        assert "404" in str(http_error.value)
        time.sleep(0.3)
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    captured = capsys.readouterr()
    assert 'GET / HTTP/1.1" 404 Not Found' in captured.out
    assert "Logging error" not in captured.err and "TypeError" not in captured.err
