"""Audit L10: stale text and missing documentation. Every environment variable this stack added is listed in .env.example; the text the audit named is corrected;
slot_cap no longer raises on an infinite setting."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mba_bot import multi_trade as mt

ROOT = Path(__file__).resolve().parent.parent

NEW_SETTINGS = """
STRATEGY_GUARDS_MODE NEW_STRATEGY_GUARDS_MODE NEW_STRATEGY_ENTRY_WINDOW_MINUTES EMA_MIN_GAP_ATR EMA_STOP_COOLDOWN_HOURS FEE_GUARD_MARGIN FEE_GUARD_FEE_BPS
FEE_GUARD_SLIPPAGE_BPS FEE_GUARD_EXPECTED_MOVE_ATR MAX_OPEN_POSITIONS MAX_TOTAL_RISK_PCT STRATEGY_DAILY_LOSS_PCT STRATEGY_PAUSE_AFTER_LOSSES STRATEGY_STREAK_PAUSE_HOURS
STRATEGY_EXPECTANCY_WINDOW OWNER_ALERT_CHAT_ID GRADE2_MIN_TRADES GRADE3_MIN_TRADES GRADE_EXTREME_ATR GRADE_DAILY_LOSS_FRACTION PAPER_MIN_NOTIONAL
RSI_MR_ENABLED RSI_MR_PAPER_ONLY RSI_MR_TIMEFRAME RSI_MR_RISK_PCT RSI_MR_ADX_MAX RSI_MR_STOP_ATR RSI_MR_TARGET_ATR RSI_MR_TIME_STOP_CANDLES RSI_MR_PAUSE_AFTER_LOSSES
RSI_MR_SPOT_FEE_BPS RSI_MR_FUTURES_FEE_BPS RSI_MR_SLIPPAGE_BPS
DONCHIAN_ENABLED DONCHIAN_PAPER_ONLY DONCHIAN_TIMEFRAME DONCHIAN_RISK_PCT DONCHIAN_ENTRY_PERIOD DONCHIAN_EXIT_PERIOD DONCHIAN_VOLUME_MULT DONCHIAN_ATR_EXTREME_MULT
DONCHIAN_STOP_ATR DONCHIAN_TRAIL_ATR DONCHIAN_STOP_COOLDOWN_HOURS DONCHIAN_SPOT_FEE_BPS DONCHIAN_FUTURES_FEE_BPS DONCHIAN_SLIPPAGE_BPS
API_SINGLE_INSTANCE_LOCK
""".split()


def test_every_new_setting_is_in_env_example_and_is_a_real_setting():
    example = (ROOT / ".env.example").read_text()
    config = (ROOT / "src/mba_bot/config.py").read_text()
    for name in NEW_SETTINGS:
        assert re.search(rf"^#?\s*{name}=", example, re.M), f"{name} is missing from .env.example"
        assert f'"{name}"' in config, f"{name} is not read by config.py (the list in this test is stale)"


def test_the_defaults_in_env_example_are_the_code_defaults_and_safe():
    from mba_bot.config import load_settings
    s = load_settings()
    example = (ROOT / ".env.example").read_text()
    assert re.search(r"^#?\s*RSI_MR_ENABLED=false", example, re.M) and re.search(r"^#?\s*DONCHIAN_ENABLED=false", example, re.M)
    assert re.search(r"^#?\s*NEW_STRATEGY_GUARDS_MODE=off", example, re.M) and re.search(r"^#?\s*STRATEGY_GUARDS_MODE=off", example, re.M)
    assert re.search(r"^#?\s*MAX_OPEN_POSITIONS=1\b", example, re.M)
    assert s.rsi_mr_enabled is False and s.donchian_enabled is False and s.new_strategy_guards_mode == "off" and s.max_open_positions == 1
    # the lock default is NOT changed by this stack: .env.example only recommends it
    assert s.api_single_instance_lock is False
    assert re.search(r"API_SINGLE_INSTANCE_LOCK=true", example) and "owner" in example.lower()


def test_slot_cap_does_not_raise_on_infinity_or_nan():
    for bad in (float("inf"), float("-inf"), float("nan")):
        assert mt.configured_cap(bad) == 1
        assert mt.slot_cap(is_paper=True, configured=bad, grade=3) == 1


def test_the_stale_statements_are_gone():
    guards = (ROOT / "docs/safety/strategy_guards.md").read_text()
    multi = (ROOT / "docs/safety/multi_trade.md").read_text()
    assert "One position per account is a database guarantee (unique index on non-terminal trades)" not in guards
    assert "Fee guard on every entry:" not in guards and "enforce mode" in guards
    assert "pool all of the account's trades" not in multi and "per strategy" in multi
    assert "Auto Select is ignored for an account that uses a pair list." not in multi
    src = (ROOT / "src/mba_bot/strategy_guards.py").read_text()
    assert "`fee_guard_fee_bps` per side on entry and exit notional." not in src
