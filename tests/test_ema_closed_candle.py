"""Golden test: with EMA_CLOSED_CANDLE_ONLY at its default (off) the single-trade cycle behaves exactly as on main before this change.

The literals below were recorded by running the unchanged code (commit 5c91bde) on the fixed fixture in ema_closed_helpers.py."""
import pytest
from ema_closed_helpers import HOUR, T0, replay

GOLDEN_SIGNALS = [(9, 'bullish_crossover'), (13, 'bearish_crossover'), (17, 'bullish_crossover'), (22, 'bearish_crossover'), (27, 'bullish_crossover')]
GOLDEN_TRADE_COUNTS = [0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3]
GOLDEN_FINAL_TRADES = [['long', 'closed', 'crossover_exit', 98.0, 100.0], ['long', 'closed', 'crossover_exit', 97.0, 101.0], ['long', 'open', None, 100.0, None]]
GOLDEN_DECISIONS = [('entry', 'accepted', 'entry_paper', 'BTC/USDT', 1780304400000, 'bullish_crossover'), ('entry', 'accepted', 'entry_paper', 'BTC/USDT', 1780333200000, 'bullish_crossover'), ('entry', 'accepted', 'entry_paper', 'BTC/USDT', 1780369200000, 'bullish_crossover'), ('signal', 'accepted', 'signal_actionable', 'BTC/USDT', 1780304400000, 'bullish_crossover'), ('signal', 'accepted', 'signal_actionable', 'BTC/USDT', 1780333200000, 'bullish_crossover'), ('signal', 'accepted', 'signal_actionable', 'BTC/USDT', 1780369200000, 'bullish_crossover')]


def test_default_off_matches_main(tmp_path, monkeypatch):
    steps, decisions, _ = replay(tmp_path, monkeypatch)
    assert [(k, s) for k, s, _ in steps if s != "none"] == GOLDEN_SIGNALS
    assert [len(t) for _, _, t in steps] == GOLDEN_TRADE_COUNTS
    assert [list(map(tuple, steps[-1][2]))] == [list(map(tuple, GOLDEN_FINAL_TRADES))]
    assert decisions == GOLDEN_DECISIONS


def _by_step(steps):
    return {k: (sig, trades) for k, sig, trades in steps}


def _rows(decisions, **kw):
    keys = ("stage", "outcome", "reason", "pair", "candle_ts", "signal")
    return [d for d in decisions if all(d[keys.index(k)] == v for k, v in kw.items())]


ON = {"ema_closed_candle_only": True}


def test_off_acts_on_the_forming_candle(tmp_path, monkeypatch):
    steps, _, _ = replay(tmp_path, monkeypatch, offsets=(5,))
    by = _by_step(steps)
    assert by[9][0] == "bullish_crossover" and len(by[9][1]) == 1  # candle 9 is still forming and already opened a trade


def test_on_ignores_a_crossover_that_exists_only_on_the_forming_candle(tmp_path, monkeypatch):
    steps, _, _ = replay(tmp_path, monkeypatch, overrides=ON, offsets=(5,))
    by = _by_step(steps)
    assert by[9] == ("none", [])  # same data as above, the forming candle is dropped
    assert by[10][0] == "bullish_crossover"  # the same crossover, once candle 9 has closed
    assert [t[:2] for t in by[10][1]] == [("long", "open")]
    assert by[10][1][0][3] == 102.0  # priced from the live last row (candle 10's close), not from the closed candle's old close of 98


def test_on_exit_waits_for_the_close(tmp_path, monkeypatch):
    off, _, _ = replay(tmp_path, monkeypatch, offsets=(5,))
    assert _by_step(off)[13][1][0][1:3] == ("closed", "crossover_exit")  # off: the forming candle 13 closes the trade
    on, _, _ = replay(tmp_path.parent / (tmp_path.name + "_on"), monkeypatch, overrides=ON, offsets=(5,))
    by = _by_step(on)
    assert by[13][1][0][1] == "open"  # on: candle 13 has not closed, no crossover exit yet
    assert by[14][1][0][1:3] == ("closed", "crossover_exit")  # exit is NOT window-limited: it acts on the first poll after the close


def test_on_stale_entry_is_skipped_and_logged_once_per_candle(tmp_path, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="mba_bot.trader")
    steps, decisions, _ = replay(tmp_path, monkeypatch, overrides=ON, offsets=(30, 35, 40))
    assert len([r for r in caplog.records if "signal_stale" in r.getMessage()]) == 3  # the log line is deduplicated too
    assert {d[4] for d in _rows(decisions, reason="signal_stale")} == {T0 + 9 * HOUR, T0 + 17 * HOUR, T0 + 27 * HOUR}  # keyed on the closed signal candle
    assert all(trades == [] for _, _, trades in steps[:1]) and steps[-1][2] == []  # a crossover seen 30 minutes after the close is never entered
    stale = _rows(decisions, outcome="skipped", reason="signal_stale")
    assert len(stale) == 3  # three crossovers, three polls each, one row per candle
    assert not _rows(decisions, stage="entry", outcome="accepted")
    assert len({d[4] for d in stale}) == 3


def test_on_entry_inside_the_window_is_logged_once_per_candle(tmp_path, monkeypatch):
    _, decisions, _ = replay(tmp_path, monkeypatch, overrides=ON, offsets=(5, 10, 15))
    signal_rows = _rows(decisions, stage="signal", outcome="accepted")
    assert len(signal_rows) == len({d[4] for d in signal_rows}) == 3  # one row per signal candle across three polls each
    assert T0 + 9 * HOUR in {d[4] for d in signal_rows}  # stamped with the CLOSED candle that carried the crossover


def test_window_edge(tmp_path, monkeypatch):
    inside, _, _ = replay(tmp_path, monkeypatch, overrides=dict(ON, ema_entry_window_minutes=10.0), offsets=(10,))
    assert _by_step(inside)[10][1][:1] and _by_step(inside)[10][1][0][1] == "open"
    outside, _, _ = replay(tmp_path.parent / (tmp_path.name + "_o"), monkeypatch, overrides=dict(ON, ema_entry_window_minutes=10.0), offsets=(11,))
    assert _by_step(outside)[10][1] == []


def test_unsupported_timeframe_fails_closed(tmp_path, monkeypatch):
    steps, decisions, _ = replay(tmp_path, monkeypatch, overrides=dict(ON, timeframe="1M"), offsets=(5,))
    assert all(sig == "none" and trades == [] for _, sig, trades in steps)


def test_helper_unit():
    from mba_bot import trader
    from helpers_live import make_settings
    from mba_bot.strategy import Candle

    cs = [Candle(timestamp=T0 + i * HOUR, open=1, high=1, low=1, close=1, volume=1) for i in range(5)]
    now_ms = T0 + 4 * HOUR + 1  # candle 4 has just started
    off = make_settings(ema_closed_candle_only=False)
    on = make_settings(ema_closed_candle_only=True)
    assert trader._ema_signal_candles(cs, off, now_ms) is cs
    assert trader._ema_signal_candles(cs, on, now_ms) == cs[:4]
    assert trader._ema_signal_candles(cs, on, T0 + 4 * HOUR) == cs[:4]  # a candle closing exactly now counts as closed


# ------------------------------------------------------------------ the other call sites: multi-trade cycle and Auto Select
def _multi_hook(aid):
    from mba_bot.db import session_scope
    from mba_bot.models import TradingAccount

    with session_scope() as s:
        s.get(TradingAccount, aid).pair_list = "ETH/USDT"


def _auto_hook(aid):
    from mba_bot.db import session_scope
    from mba_bot.models import TradingAccount

    with session_scope() as s:
        s.get(TradingAccount, aid).pair_selection_mode = "auto"


@pytest.mark.parametrize("hook,extra", [(_multi_hook, {"max_open_positions": 2}), (_auto_hook, {})])
def test_multi_and_auto_paths_follow_the_switch(tmp_path, monkeypatch, hook, extra):
    off, _, _ = replay(tmp_path / "off", monkeypatch, overrides=extra, offsets=(5,), pre_hook=hook)
    on, dec_on, _ = replay(tmp_path / "on", monkeypatch, overrides=dict(extra, **ON), offsets=(5,), pre_hook=hook)
    assert _by_step(off)[9][1] != []  # off: the forming candle opens a trade
    assert _by_step(on)[9][1] == []  # on: nothing until candle 9 has closed
    assert _by_step(on)[10][1] != [] and _by_step(on)[10][1][0][1] == "open"
    assert _by_step(on)[13][1][0][1] == "open" and _by_step(on)[14][1][0][1] == "closed"  # the exit also waits for the close
    assert _by_step(off)[13][1][0][1] == "closed"
    late, _, _ = replay(tmp_path / "late", monkeypatch, overrides=dict(extra, **ON), offsets=(30,), pre_hook=hook)
    assert all(trades == [] for _, _, trades in late)  # the stale-entry guard applies on these paths too


# ------------------------------------------------------------------ live paths: the open-trade monitor and the multi-live recovery monitor
def _stamped(ohlcv, *, minutes_since_open, hours_from_end=0):
    """The fixture series re-dated so its LAST candle opened `minutes_since_open` ago (still forming with a 1h timeframe if under 60)."""
    import time

    last = int(time.time() * 1000) - minutes_since_open * 60_000
    n = len(ohlcv)
    return [[last - (n - 1 - i) * HOUR] + list(r[1:]) for i, r in enumerate(ohlcv)]


@pytest.fixture
def db(tmp_path):
    from mba_bot.db import init_db

    init_db(f"sqlite:///{tmp_path}/live.db")
    return tmp_path


def test_live_monitor_crossover_exit_waits_for_the_close(db, monkeypatch):
    import dataclasses

    from helpers_live import BEAR_PRICE, BEARISH_OHLCV, open_protected, run_cycle, the_trade

    client, settings, aid = open_protected(monkeypatch, market="spot")
    on = dataclasses.replace(settings, ema_closed_candle_only=True)
    client._ohlcv, client.price = _stamped(BEARISH_OHLCV, minutes_since_open=5), BEAR_PRICE  # the opposite cross exists only on the forming candle
    run_cycle(aid, on)
    assert the_trade(aid).status == "open"  # on: no exit on a forming candle
    client._ohlcv = _stamped(BEARISH_OHLCV, minutes_since_open=65)  # the same candle after it closed
    run_cycle(aid, on)
    assert the_trade(aid).status == "closed" and the_trade(aid).close_reason == "crossover_exit"


def test_live_monitor_off_still_exits_on_the_forming_candle(db, monkeypatch):
    from helpers_live import BEAR_PRICE, BEARISH_OHLCV, open_protected, run_cycle, the_trade

    client, settings, aid = open_protected(monkeypatch, market="spot")
    client._ohlcv, client.price = _stamped(BEARISH_OHLCV, minutes_since_open=5), BEAR_PRICE
    run_cycle(aid, settings)
    assert the_trade(aid).status == "closed" and the_trade(aid).close_reason == "crossover_exit"  # today's behaviour, unchanged


@pytest.mark.parametrize("switch,expected", [(True, [False, False]), (False, [True, True])])
def test_live_recovery_monitor_follows_the_switch(db, monkeypatch, switch, expected):
    import dataclasses

    from helpers_live import BEARISH_OHLCV, run_cycle
    from test_multi_trade import live_trades_on_pairs

    from mba_bot import trader

    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, 2)
    st = dataclasses.replace(st, ema_closed_candle_only=switch)
    seen = []

    async def spy_monitor(ctx, trade, *, crossover_exit, current_price):
        seen.append(crossover_exit)

    monkeypatch.setattr(trader.lifecycle, "monitor_trade", spy_monitor)
    for f in fakes.values():
        f._ohlcv = _stamped(BEARISH_OHLCV, minutes_since_open=5)
    run_cycle(aid, st)
    assert seen == expected


def test_switch_default_is_off_in_code_and_env(monkeypatch):
    import dataclasses

    from mba_bot.config import Settings, load_settings

    monkeypatch.delenv("EMA_CLOSED_CANDLE_ONLY", raising=False)
    assert load_settings().ema_closed_candle_only is False
    assert next(f for f in dataclasses.fields(Settings) if f.name == "ema_closed_candle_only").default is False
    monkeypatch.setenv("EMA_CLOSED_CANDLE_ONLY", "true")
    assert load_settings().ema_closed_candle_only is True


def test_stale_helper_rejects_a_candle_that_has_not_closed_yet():
    from helpers_live import make_settings
    from types import SimpleNamespace

    from mba_bot import trader
    from mba_bot.strategy import Candle, SignalType

    st = make_settings(ema_closed_candle_only=True)
    sig = SimpleNamespace(signal=SignalType.BULLISH_CROSSOVER)
    acct = SimpleNamespace(id="x", market_type="spot")
    cs = [Candle(timestamp=T0, open=1, high=1, low=1, close=1, volume=1)]
    from datetime import datetime, timezone

    kw = dict(now=datetime.now(timezone.utc), is_paper=True)
    assert trader._ema_entry_stale(None, st, acct, sig, cs, "BTC/USDT", now_ms=T0 + HOUR - 1, **kw) is True  # not closed yet (negative age)
    assert trader._ema_entry_stale(None, st, acct, SimpleNamespace(signal=SignalType.BEARISH_CROSSOVER), cs, "BTC/USDT", now_ms=T0 + HOUR - 1, **kw) is False  # Spot never shorts: not an entry
