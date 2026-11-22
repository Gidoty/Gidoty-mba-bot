"""Auto Select accounts evaluate every strategy they opted in to, every round, on every candidate pair, whether or not EMA found a signal (docs/safety/auto_select_strategies.md).

Before: the Auto Select scan returned before the Donchian and RSI checks when EMA had no actionable signal, and when EMA did, those two ran on the single pair EMA picked. Now each opted-in strategy is evaluated on each
pair the scan read, with the candles the scan already fetched. The priority (Donchian, then EMA, then RSI), the one live slot, the guards, the risk, the stops and the entry path are unchanged, and an account that has
not opted in behaves exactly as before. Fake exchange and fixed candles throughout: nothing here is evidence about Binance."""
from __future__ import annotations

import itertools
import json

import pytest
from helpers_live import BULLISH_OHLCV, all_trades, futures_client, install, make_settings, new_account, run_cycle, spot_client, the_trade
from test_donchian import base, cd
from test_donchian_integration import raw as don_raw
from test_rsi_mr import LONG as RSI_LONG
from test_rsi_mr_integration import raw as rsi_raw

from mba_bot import donchian, opportunity, rsi_mr, telemetry, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount

PAIRS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")  # the scan order
BOTH = "donchian_breakout_v1,rsi_mr_v1"
_n = itertools.count()


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    telemetry.reset_for_tests()
    opportunity.reset_for_tests()
    yield


def st(**over):
    d = dict(donchian_enabled=True, live_donchian=True, rsi_mr_enabled=True, live_rsi_mr=True, strategy_guards_mode="enforce", new_strategy_guards_mode="enforce", fee_guard_margin=0.5,
             strategy_expectancy_window=0, max_total_risk_pct=5.0, max_notional_multiplier=100.0, trade_telemetry=True, opportunity_ledger=True, protection_precheck_mode="off")
    d.update(over)
    return make_settings(**d)


DON_FLAT = don_raw(base(41))


def _trend_breakout(n=40, jump=3.8, vol=300.0):
    """A steady climb (the fast EMA is already above the slow one: no crossover) ending in a high-volume close beyond the 20-candle channel: a Donchian long and NO EMA signal."""
    cs = []
    for i in range(n):
        c = 100 + 0.1 * i
        cs.append(cd(i, c - 0.05, c + 0.5, c - 0.5, c, 100.0))
    c = 100 + 0.1 * (n - 1)
    cs.append(cd(n, c, c + jump + 0.3, c, c + jump, vol))
    return cs


DON_HIT = don_raw(_trend_breakout())
RSI_FLAT = rsi_raw([100.0] * 60)
RSI_HIT = rsi_raw(RSI_LONG)


def auto_account(settings, *, extra=BOTH, market="futures", pair="BTC/USDT"):
    aid = new_account(settings, market_type=market, pair=pair, email=f"auto{next(_n)}@example.com")
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.pair_selection_mode = "auto"
        a.extra_strategies = extra
    return aid


def rig(monkeypatch, by_pair, *, trade_pair="XRP/USDT", market="futures"):
    """The fake exchange can trade ONE pair (`trade_pair`); the candles of the others only feed the scan."""
    mk = futures_client if market == "futures" else spot_client
    kw = dict(ohlcv=DON_FLAT, ohlcv_by_pair=by_pair, pair=trade_pair, price=107.7)
    if market == "futures":
        kw["leverage_by_symbol"] = {trade_pair: 1.0}
    client = mk(**kw)
    install(monkeypatch, client)
    scans = []
    real = trader._scan_auto_pair

    async def spy(*a, **k):
        r = await real(*a, **k)
        scans.append(r)
        return r

    monkeypatch.setattr(trader, "_scan_auto_pair", spy)
    client.scans = scans  # what the EMA scan returned each round (None = no actionable EMA signal)
    return client


def pair_of(account_id):
    with session_scope() as s:
        return s.get(TradingAccount, account_id).pair


def ledger(account_id, strategy=None):
    with session_scope() as s:
        q = s.query(DecisionLog).filter(DecisionLog.account_id == account_id, DecisionLog.stage == opportunity.STAGE)
        if strategy:
            q = q.filter(DecisionLog.strategy_id == strategy)
        return q.order_by(DecisionLog.id).all()


# ------------------------------------------------------------------ the new branches
def test_donchian_is_evaluated_and_entered_on_the_pair_where_it_signals_although_ema_found_nothing(db, monkeypatch):
    settings = st(live_rsi_mr=False)
    client = rig(monkeypatch, {"BTC/USDT": DON_FLAT, "ETH/USDT": DON_FLAT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_HIT})
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert client.scans == [None]  # EMA found nothing in the whole universe
    t = the_trade(aid)
    assert t.strategy_id == "donchian_breakout_v1" and t.side == "long" and t.status == "open" and t.protection_status == "protected" and t.pair == "XRP/USDT"
    assert pair_of(aid) == "XRP/USDT"  # an Auto Select pair changes together with an entry
    rows = ledger(aid, "donchian_breakout_v1")
    assert {r.pair for r in rows if r.outcome in ("signal", "no_signal")} == set(PAIRS)  # evaluated on every candidate
    assert [r for r in ledger(aid) if r.reason_code == opportunity.R_NOT_EVALUATED] == []


def test_rsi_is_evaluated_and_entered_on_the_pair_where_it_signals_although_ema_found_nothing(db, monkeypatch):
    settings = st(live_donchian=False)
    client = rig(monkeypatch, {"BTC/USDT": RSI_FLAT, "ETH/USDT": RSI_HIT, "BNB/USDT": RSI_FLAT, "XRP/USDT": RSI_FLAT}, trade_pair="ETH/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert client.scans == [None]  # EMA found nothing in the whole universe
    t = the_trade(aid)
    assert t.strategy_id == "rsi_mr_v1" and t.pair == "ETH/USDT" and t.protection_status == "protected" and pair_of(aid) == "ETH/USDT"
    assert {r.pair for r in ledger(aid, "rsi_mr_v1") if r.outcome in ("signal", "no_signal")} == set(PAIRS)


def test_donchian_beats_rsi_when_both_signal_in_the_same_round(db, monkeypatch):
    settings = st()
    rig(monkeypatch, {"BTC/USDT": RSI_HIT, "ETH/USDT": DON_HIT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_FLAT}, trade_pair="ETH/USDT")  # each pair carries one data set: Donchian on ETH, RSI on BTC
    aid = auto_account(settings)
    run_cycle(aid, settings)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].strategy_id == "donchian_breakout_v1" and trades[0].pair == "ETH/USDT"  # the code's order: Donchian, then EMA, then RSI; one live slot


def test_rsi_takes_the_slot_when_donchian_has_nothing(db, monkeypatch):
    settings = st()
    rig(monkeypatch, {"BTC/USDT": DON_FLAT, "ETH/USDT": RSI_HIT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_FLAT}, trade_pair="ETH/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert [t.strategy_id for t in all_trades(aid)] == ["rsi_mr_v1"]


def test_two_pairs_signal_the_first_in_scan_order_is_entered_and_the_other_is_recorded_as_not_taken(db, monkeypatch):
    settings = st(live_rsi_mr=False)
    rig(monkeypatch, {"BTC/USDT": DON_FLAT, "ETH/USDT": DON_HIT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_HIT}, trade_pair="ETH/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert the_trade(aid).pair == "ETH/USDT"
    skipped = [r for r in ledger(aid, "donchian_breakout_v1") if r.reason_code == opportunity.R_NOT_BEST]
    assert [r.pair for r in skipped] == ["XRP/USDT"] and json.loads(skipped[0].detail_json)["picked"] == "ETH/USDT"


def test_donchian_has_priority_over_an_ema_pick_on_another_pair(db, monkeypatch):
    settings = st(live_rsi_mr=False)
    rig(monkeypatch, {"BTC/USDT": BULLISH_OHLCV, "ETH/USDT": DON_FLAT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_HIT})  # EMA crosses up on BTC; Donchian breaks out on XRP
    aid = auto_account(settings)
    run_cycle(aid, settings)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].strategy_id == "donchian_breakout_v1" and trades[0].pair == "XRP/USDT"


def test_with_an_ema_signal_and_no_donchian_hit_ema_still_enters_and_rsi_waits_for_the_slot(db, monkeypatch):
    settings = st()
    rig(monkeypatch, {"BTC/USDT": BULLISH_OHLCV, "ETH/USDT": DON_FLAT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_FLAT}, trade_pair="BTC/USDT")
    aid = auto_account(settings)
    seen = []
    real = trader._auto_new_strategy_pass

    async def spy(*a, **k):
        seen.append(k["strategy"])
        return await real(*a, **k)

    monkeypatch.setattr(trader, "_auto_new_strategy_pass", spy)
    run_cycle(aid, settings)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].strategy_id.startswith("ema") and trades[0].pair == "BTC/USDT"
    assert seen == ["donchian_breakout_v1"]  # EMA took the one live slot: the RSI pass is not run


def test_a_refused_entry_leaves_the_pair_as_it_was(db, monkeypatch):
    settings = st(live_rsi_mr=False, fee_guard_margin=0.0)  # the new strategies need the fee guard: the entry is refused
    client = rig(monkeypatch, {"BTC/USDT": DON_FLAT, "ETH/USDT": DON_FLAT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_HIT})
    aid = auto_account(settings, pair="BTC/USDT")
    run_cycle(aid, settings)
    assert all_trades(aid) == [] and client.orders == {} and pair_of(aid) == "BTC/USDT"


def test_one_pairs_failed_check_does_not_stop_the_others(db, monkeypatch):
    settings = st(live_rsi_mr=False)
    rig(monkeypatch, {"BTC/USDT": DON_FLAT, "ETH/USDT": DON_HIT, "BNB/USDT": DON_FLAT, "XRP/USDT": DON_FLAT}, trade_pair="ETH/USDT")
    real = trader._live_strategy_signal

    async def flaky(client, account, pair, *a, **k):
        if pair == "BTC/USDT":
            raise RuntimeError("boom")
        return await real(client, account, pair, *a, **k)

    monkeypatch.setattr(trader, "_live_strategy_signal", flaky)
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert the_trade(aid).pair == "ETH/USDT"


def test_a_strategy_that_could_not_be_evaluated_is_still_recorded_as_not_evaluated(db, monkeypatch):
    settings = st(live_rsi_mr=False)
    rig(monkeypatch, {p: DON_FLAT for p in PAIRS}, trade_pair="BTC/USDT")

    async def always_fails(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(trader, "_live_strategy_signal", always_fails)
    aid = auto_account(settings)
    run_cycle(aid, settings)
    rows = [r for r in ledger(aid) if r.reason_code == opportunity.R_NOT_EVALUATED]
    assert [r.strategy_id for r in rows] == ["donchian_breakout_v1"]


# ------------------------------------------------------------------ accounts that have not opted in behave as today
def test_who_runs_the_new_path_only_opted_in_live_auto_accounts():
    class A:
        pair_selection_mode, extra_strategies = "auto", BOTH

    a = A()
    assert trader._auto_new_strategies(st(), a, False) == ["donchian_breakout_v1", "rsi_mr_v1"]  # the code's priority order
    assert trader._auto_new_strategies(st(), a, True) == []  # paper
    assert trader._auto_new_strategies(st(live_donchian=False), a, False) == ["rsi_mr_v1"]
    assert trader._auto_new_strategies(st(donchian_enabled=False, rsi_mr_enabled=False), a, False) == []  # strategy switches off
    a.extra_strategies = None
    assert trader._auto_new_strategies(st(), a, False) == []  # not opted in
    a.extra_strategies, a.pair_selection_mode = BOTH, "manual"
    assert trader._auto_new_strategies(st(), a, False) == []  # manual accounts keep their own path


@pytest.mark.parametrize("data", ["flat", "donchian_hit", "ema_cross"])
def test_an_account_that_has_not_opted_in_never_reaches_the_new_code_and_reads_no_extra_candles(db, monkeypatch, data):
    by = {"flat": {p: DON_FLAT for p in PAIRS}, "donchian_hit": {**{p: DON_FLAT for p in PAIRS}, "XRP/USDT": DON_HIT}, "ema_cross": {**{p: DON_FLAT for p in PAIRS}, "BTC/USDT": BULLISH_OHLCV}}[data]
    settings = st()
    client = rig(monkeypatch, by, trade_pair="BTC/USDT")
    called = []

    async def spy(*a, **k):
        called.append(1)

    monkeypatch.setattr(trader, "_auto_new_strategy_pass", spy)
    monkeypatch.setattr(trader, "_live_strategy_signal", spy)
    aid = auto_account(settings, extra=None)
    run_cycle(aid, settings)
    assert called == []
    assert [t.strategy_id for t in all_trades(aid) if not t.strategy_id.startswith("ema")] == []  # no Donchian or RSI trade
    assert sorted(client.fetched_pairs) == sorted(PAIRS)  # exactly the scan's four reads, as before
    assert [r for r in ledger(aid) if r.strategy_id in ("donchian_breakout_v1", "rsi_mr_v1")] == []


def test_an_opted_in_account_whose_strategy_switches_are_off_is_also_unchanged(db, monkeypatch):
    settings = st(live_donchian=False, live_rsi_mr=False)
    client = rig(monkeypatch, {**{p: DON_FLAT for p in PAIRS}, "XRP/USDT": DON_HIT})
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert all_trades(aid) == [] and sorted(client.fetched_pairs) == sorted(PAIRS)


# ------------------------------------------------------------------ candle reads
def test_no_extra_candle_reads_on_a_round_where_ema_found_nothing(db, monkeypatch):
    settings = st()
    client = rig(monkeypatch, {p: DON_FLAT for p in PAIRS}, trade_pair="BTC/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert client.scans == [None] and sorted(client.fetched_pairs) == sorted(PAIRS)  # the scan's own four reads serve EMA, Donchian and RSI: zero added


def test_with_an_ema_signal_the_two_extra_single_pair_reads_of_before_are_gone(db, monkeypatch):
    settings = st(live_donchian=False)  # keep Donchian out of it: RSI would have fetched account.pair again before
    client = rig(monkeypatch, {"BTC/USDT": BULLISH_OHLCV, **{p: DON_FLAT for p in PAIRS[1:]}}, trade_pair="BTC/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert all_trades(aid)[0].strategy_id.startswith("ema") and sorted(client.fetched_pairs) == sorted(PAIRS)  # entered on EMA, the RSI pass skipped because the slot is taken


def test_a_strategy_on_another_timeframe_adds_one_read_per_candidate_pair_for_that_strategy_only(db, monkeypatch):
    settings = st(live_rsi_mr=False, donchian_timeframe="4h")
    client = rig(monkeypatch, {p: DON_FLAT for p in PAIRS}, trade_pair="BTC/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert len(client.fetched_pairs) == 8 and sorted(set(client.fetched_pairs)) == sorted(PAIRS)  # 4 scan reads + 4 Donchian reads at its own timeframe
