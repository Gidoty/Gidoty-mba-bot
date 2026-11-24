"""Part E: reversal exits for EMA trades (docs/safety/ema_reversal_exits.md). Off by default.

PROVES: the two pure rules (a close back across the fast EMA after the trade was in profit; the fast/slow gap shrinking by a set fraction of its peak since entry) for long and short, their edges, that a candle from before
the entry never counts, that nothing happens with the rules off or for a trade of another strategy, that a live EMA trade exits through lifecycle.execute_exit with its own close reason, and the paper close.
DOES NOT PROVE: that either rule makes money (Part H measures that)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers_live import run_cycle, the_trade
from helpers_profit import HOUR, candles, live

from mba_bot import config, ema_reversal as er, trader
from mba_bot.db import init_db

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def C(ts, close):
    return SimpleNamespace(timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=1.0)


# ---------------------------------------------------------------------------------------------- pure
def test_a_close_back_across_the_fast_ema_ends_a_trade_that_was_in_profit():
    k = dict(entry=100.0, initial_stop=90.0, min_profit_r=0.5)
    assert er.fast_cross_hit("long", close=104.0, fast=105.0, peak=106.0, **k) is True  # peak +0.6 R, close below the fast EMA
    assert er.fast_cross_hit("long", close=105.0, fast=105.0, peak=106.0, **k) is False  # on it, not across it
    assert er.fast_cross_hit("long", close=104.0, fast=105.0, peak=104.9, **k) is False  # never in profit by 0.5 R: not this rule's job
    assert er.fast_cross_hit("long", close=104.0, fast=105.0, peak=105.0, **k) is True  # exactly 0.5 R counts
    assert er.fast_cross_hit("short", close=96.0, fast=95.0, peak=94.0, entry=100.0, initial_stop=110.0, min_profit_r=0.5) is True
    assert er.fast_cross_hit("short", close=94.0, fast=95.0, peak=94.0, entry=100.0, initial_stop=110.0, min_profit_r=0.5) is False
    assert er.fast_cross_hit("long", close=104.0, fast=None, peak=110.0, **k) is False and er.fast_cross_hit("long", close=104.0, fast=105.0, peak=None, **k) is False
    assert er.fast_cross_hit("long", close=104.0, fast=105.0, peak=110.0, entry=100.0, initial_stop=100.0, min_profit_r=0.5) is False  # R unusable


def test_the_gap_rule_fires_when_the_gap_has_shrunk_by_the_set_fraction_of_its_peak():
    assert er.gap_shrink_hit("long", gap_now=5.0, gap_peak=10.0, fraction=0.5) is True
    assert er.gap_shrink_hit("long", gap_now=5.01, gap_peak=10.0, fraction=0.5) is False
    assert er.gap_shrink_hit("long", gap_now=-1.0, gap_peak=10.0, fraction=0.5) is True  # crossed over entirely
    assert er.gap_shrink_hit("long", gap_now=0.0, gap_peak=0.0, fraction=0.5) is False and er.gap_shrink_hit("long", gap_now=0.0, gap_peak=-2.0, fraction=0.5) is False  # no gap in the trade's favour yet
    assert er.gap_shrink_hit("long", gap_now=1.0, gap_peak=10.0, fraction=0.0) is False  # off
    assert er.gap_shrink_hit("long", gap_now=None, gap_peak=10.0, fraction=0.5) is False


def series(closes, start_ts=1000, step=10):
    return [C(start_ts + i * step, c) for i, c in enumerate(closes)]


RISING_THEN_FALLING = [100.0] * 12 + [101, 103, 106, 110, 115, 121, 127, 133, 139, 145, 150, 154, 157, 159, 160] + [150, 140, 130, 120]


def test_evaluate_reads_the_closed_candles_and_returns_the_reason_or_none():
    cs = series(RISING_THEN_FALLING)
    opened = cs[12].timestamp
    on_fast = er.ReversalRules(fast_cross=True, min_profit_r=0.5)
    on_gap = er.ReversalRules(gap_fraction=0.5)
    off = er.ReversalRules()
    peak = 160.0
    k = dict(side="long", entry=101.0, initial_stop=95.0, opened_ms=opened, fast_period=3, slow_period=8, peak=peak)
    assert er.evaluate(closed=cs[:27], rules=on_fast, **k) is None  # still rising: above the fast EMA
    assert er.evaluate(closed=cs, rules=on_fast, **k) == "reversal_fast_ema"
    assert er.evaluate(closed=cs, rules=off, **k) is None
    hits = [n for n in range(len(cs) - 8, len(cs) + 1) if er.evaluate(closed=cs[:n], rules=on_gap, **k) == "reversal_gap"]
    assert hits and hits[0] > 27  # the gap only shrinks after the top
    assert er.evaluate(closed=cs[:27], rules=on_gap, **k) is None
    assert er.evaluate(closed=[], rules=on_fast, **k) is None
    assert er.evaluate(closed=cs[:12], rules=on_fast, **{**k, "opened_ms": cs[-1].timestamp + 10}) is None  # no candle has closed after the entry


def test_a_candle_from_before_the_entry_never_counts_for_the_gap_peak():
    cs = series([100.0] * 10 + [130, 160, 190, 200] + [150, 120, 100, 95, 92, 90])  # a huge gap BEFORE the entry
    entered_late = cs[14].timestamp
    k = dict(side="long", entry=150.0, initial_stop=140.0, opened_ms=entered_late, fast_period=3, slow_period=8, peak=150.0, rules=er.ReversalRules(gap_fraction=0.5))
    got_late = er.evaluate(closed=cs, **k)
    k_early = {**k, "opened_ms": cs[10].timestamp}
    got_early = er.evaluate(closed=cs, **k_early)
    assert got_early == "reversal_gap"  # judged from the early entry the gap peak is large and has collapsed
    assert got_late in (None, "reversal_gap") and (got_late is None or er.evaluate(closed=cs[:16], **k) is not None)  # from the late entry the early gap is not part of the peak


def test_a_short_mirrors_both_rules():
    falling = [100.0] * 12 + [99, 97, 94, 90, 85, 79, 73, 67, 61, 55, 50, 46, 43, 41, 40] + [50, 60, 70, 80]
    cs = series(falling)
    k = dict(side="short", entry=99.0, initial_stop=105.0, opened_ms=cs[12].timestamp, fast_period=3, slow_period=8, peak=40.0)
    assert er.evaluate(closed=cs, rules=er.ReversalRules(fast_cross=True), **k) == "reversal_fast_ema"
    assert er.evaluate(closed=cs[:27], rules=er.ReversalRules(fast_cross=True), **k) is None


def test_the_settings_are_off_by_default_and_a_typo_never_switches_a_reversal_rule_on(monkeypatch):
    for n in ("EMA_REVERSAL_FAST_CROSS", "EMA_REVERSAL_GAP_FRACTION", "EMA_REVERSAL_MIN_PROFIT_R"):
        monkeypatch.delenv(n, raising=False)
    s = config.load_settings()
    assert not er.rules_from_settings(s).any_on and s.ema_reversal_min_profit_r == 0.5
    for bad in ("", "no", "tru", "2", " "):
        monkeypatch.setenv("EMA_REVERSAL_FAST_CROSS", bad)
        assert config.load_settings().ema_reversal_fast_cross is False, bad
    for bad in ("abc", "", "-1", "0", "nan", "inf", " ", "50%"):
        monkeypatch.setenv("EMA_REVERSAL_GAP_FRACTION", bad)
        assert config.load_settings().ema_reversal_gap_fraction == 0.0, bad
    monkeypatch.setenv("EMA_REVERSAL_GAP_FRACTION", "7")
    assert config.load_settings().ema_reversal_gap_fraction == 0.95


# ---------------------------------------------------------------------------------------------- live
ROWS_TOP_THEN_DROP = [(95, 100, 92, 99), (99, 140, 98, 139), (139, 141, 130, 138), (138, 138, 100, 105)]


def test_a_live_ema_trade_exits_through_execute_exit_when_a_closed_candle_falls_back_across_the_fast_ema(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_reversal_fast_cross=True, ema_fast_period=3, ema_slow_period=6)
    candles(client, base, ROWS_TOP_THEN_DROP)
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "reversal_fast_ema" and client.position == 0.0 and len(client.orders_by_role("exit")) == 1 and client.orders_by_role("stop")[0]["status"] == "canceled"
    assert any("EMA reversal" in x for x in n.texts)


def test_nothing_happens_with_the_rules_off_or_before_the_trade_was_in_profit(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_fast_period=3, ema_slow_period=6)  # rules off
    candles(client, base, ROWS_TOP_THEN_DROP)
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open" and not client.orders_by_role("exit")


def test_a_trade_that_never_reached_the_profit_level_is_not_closed_by_the_fast_ema_rule(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_reversal_fast_cross=True, ema_reversal_min_profit_r=2.0, ema_fast_period=3, ema_slow_period=6)  # +2 R = 184: never reached
    candles(client, base, ROWS_TOP_THEN_DROP)
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open"


def test_a_donchian_trade_is_never_judged_by_the_ema_reversal_rules(db, monkeypatch):
    now_ms = int(datetime.now(U).timestamp() * 1000)
    start = now_ms - 40 * HOUR
    cs = series(RISING_THEN_FALLING, start_ts=start, step=HOUR)
    t = SimpleNamespace(strategy_id="donchian_breakout_v1", initial_stop_price=95.0, opened_at=datetime.fromtimestamp((start + 12 * HOUR) / 1000, U), side="long", entry_price=100.0, peak_price=160.0)
    s = __import__("dataclasses").replace(config.load_settings(), ema_reversal_fast_cross=True, ema_fast_period=3, ema_slow_period=6)
    assert trader._reversal_reason(t, cs, s, now_ms, 120.0) is None
    t.strategy_id = None  # an EMA trade (no strategy id)
    assert trader._reversal_reason(t, cs, s, now_ms, 120.0) == "reversal_fast_ema"
    t.initial_stop_price = None
    assert trader._reversal_reason(t, cs, s, now_ms, 120.0) is None  # R cannot be measured


# ---------------------------------------------------------------------------------------------- paper
def test_a_paper_trade_is_closed_at_the_price_with_the_reversal_reason():
    class N:
        texts = []

        async def send(self, target, text):
            self.texts.append(text)

    now = datetime.now(U)
    t = SimpleNamespace(side="long", entry_price=100.0, quantity=2.0, stop_loss_price=90.0, pair="BTC/USDT", strategy_id=None, status="open", exit_price=None, pnl=None, closed_at=None, close_reason=None)
    acct = SimpleNamespace(paper_balance=1000.0, notification_target=None, pair="BTC/USDT")
    n = N()
    asyncio.run(trader._paper_close_at_price(SimpleNamespace(commit=lambda: None), acct, t, 130.0, "reversal_gap", "EMA reversal", config.load_settings(), n, now=now))
    assert t.status == "closed" and t.close_reason == "reversal_gap" and t.exit_price == 130.0 and t.pnl == 60.0 and acct.paper_balance == 1060.0 and "EMA reversal" in n.texts[0]


def test_the_gap_rule_matches_an_independent_reading_on_random_walks_for_both_sides_and_every_entry():
    import random

    from mba_bot.indicators import ema

    rng = random.Random(7)
    price, closes = 100.0, []
    for _ in range(260):
        price *= 1 + rng.gauss(0, 0.012) + (0.002 if rng.random() < 0.5 else -0.001)
        closes.append(price)
    cs = series(closes)
    fast, slow = ema(closes, 5), ema(closes, 13)
    fired = {"long": 0, "short": 0}
    for side in ("long", "short"):
        for frac in (0.3, 0.6):
            for entry_idx in range(30, 230, 17):
                for n in range(entry_idx + 1, 260, 11):
                    got = er.evaluate(side=side, entry=100.0, initial_stop=90.0 if side == "long" else 110.0, opened_ms=cs[entry_idx].timestamp, closed=cs[:n], fast_period=5, slow_period=13, peak=100.0, rules=er.ReversalRules(gap_fraction=frac))
                    sgn = 1 if side == "long" else -1
                    gaps = [sgn * (fast[i] - slow[i]) for i in range(entry_idx, n)]
                    peak_gap = max(gaps)
                    want = "reversal_gap" if (peak_gap > 0 and gaps[-1] <= (1 - frac) * peak_gap) else None
                    assert got == want, (side, frac, entry_idx, n)
                    fired[side] += got is not None
    assert fired["long"] > 5 and fired["short"] > 5  # the data exercises both outcomes for both sides
