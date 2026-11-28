"""The owner-run Testnet check (ops/testnet/check_second_stop.py), driven here against the FAKE exchange only.

PROVES: the script's sequence (open, stop A closePosition, refused second closePosition stop with the exact code and text, reduceOnly stop, cancel, closePosition while reduceOnly rests, close, what rests on a flat
position), that it leaves nothing resting and a flat position, that it refuses to start when a position or an order already exists, that a failed step still cleans up, and that it can only be given keys at a hidden
prompt (no key argument, no environment variable, no file) and only builds a demo-routed client.
DOES NOT PROVE: anything about Binance. The script is for the owner to run on the demo system."""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import ccxt
from helpers_live import futures_client

from mba_bot import venue as venue_mod

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ops" / "testnet"))
import check_second_stop as chk  # noqa: E402


def make(client):
    v = venue_mod.build_venue(client, market_type="futures", pair="BTC/USDT", entries_allowed=True)
    return v


def run(client, **kw):
    report = chk.Report(say=lambda *_: None)
    ok = asyncio.run(chk.run_check(client, make(client), report, **kw))
    return ok, report


def rows(report):
    return dict(report.rows)


def test_the_sequence_records_the_exact_refusal_and_leaves_nothing_behind():
    client = futures_client(reject_second_close_stop=True)
    ok, report = run(client)
    r = rows(report)
    assert ok is True
    assert r["2 stop A closePosition"] == "ACCEPTED"
    assert "REFUSED" in r["3 stop B second closePosition, same direction"] and "code=-4130" in r["3 stop B second closePosition, same direction"] and "class=ExchangeError" in r["3 stop B second closePosition, same direction"]
    assert r["4 stop C reduceOnly with the position quantity"] == "ACCEPTED"
    assert r["8 stop D closePosition while stop C (reduceOnly) rests"] == "ACCEPTED"
    assert client.position == 0
    assert [o for o in client.orders.values() if o["conditional"] and o["status"] == "open"] == []
    assert r["final"].startswith("position flat; conditional orders: nothing rests")


def test_when_binance_accepts_a_second_stop_the_report_says_so():
    client = futures_client(reject_second_close_stop=False)
    ok, report = run(client)
    assert "ACCEPTED (Binance did NOT refuse" in rows(report)["3 stop B second closePosition, same direction"]
    assert ok is True and client.position == 0


def test_it_refuses_to_start_on_an_existing_position_or_order_and_sends_nothing():
    client = futures_client()
    asyncio.run(client.create_order("BTC/USDT:USDT", "market", "buy", 0.01, None, {"clientOrderId": "pre-existing"}))
    before = len(client.calls)
    ok, report = run(client)
    assert ok is False and "ABORTED" in rows(report)["start"]
    assert not any(c[0] == "create_order" for c in client.calls[before:])


def test_a_failing_step_still_closes_the_position_and_cancels_the_stops():
    client = futures_client(reject_second_close_stop=True)
    client.fail("fetch_open_orders", exc=ccxt.NetworkError("boom"), times=1)  # the pre-check read fails: nothing is opened
    ok, report = run(client)
    assert "ERROR" in rows(report) and client.position == 0 and ok is True  # nothing was opened, nothing is left
    client2 = futures_client(reject_second_close_stop=True)
    # fail a read in the middle (after the position is open): the finally clause must close and cancel
    seen = {"n": 0}
    real = client2.fetch_open_orders

    async def flaky(*a, **k):
        seen["n"] += 1
        if seen["n"] == 3:
            raise ccxt.NetworkError("boom")
        return await real(*a, **k)

    client2.fetch_open_orders = flaky
    _ok2, report2 = run(client2)
    assert "ERROR" in rows(report2)
    assert client2.position == 0 and [o for o in client2.orders.values() if o["conditional"] and o["status"] == "open"] == []


def test_the_person_can_stop_it_before_any_order():
    client = futures_client()
    ok, report = run(client, confirm=lambda sym, qty: False)
    assert ok is False and "stopped by you" in rows(report)["start"]
    assert not any(c[0] == "create_order" for c in client.calls)


def test_the_report_carries_no_key_and_the_script_takes_keys_only_at_a_hidden_prompt():
    src = Path(chk.__file__).read_text()
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#",)))
    assert "getpass.getpass" in src
    for forbidden in ("os.environ", "getenv", "add_argument(\"--key", "add_argument(\"--secret", "add_argument('--key", "open(\"", "read_text(", "load_dotenv"):
        assert forbidden not in code.split("def main(")[0].replace("Path(args.out).write_text", ""), forbidden
    assert "sandbox=True" in src and "sandbox=False" not in src
    assert 'api_key=key' in src and "print(key" not in src and "print(secret" not in src
    params = inspect.signature(chk.main).parameters
    assert list(params) == ["argv"]
    ap_src = src[src.index("def main("):]
    assert "--symbol" in ap_src and "--out" in ap_src and "--key" not in ap_src and "--secret" not in ap_src
