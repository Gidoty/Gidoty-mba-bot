"""telemetry.log_decision must not drop a decision row only because a DIFFERENT strategy logged the same reason for the same pair in the same
cycle. The de-duplication key has to include the strategy id (found while testing the slot rule: only the first strategy's
`strategy_slot_lost_to` row was kept)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from helpers_live import make_settings, new_account

from mba_bot import telemetry
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def log(session, st, account, strategy_id, **kw):
    telemetry.log_decision(session, st, account, stage=telemetry.STAGE_ENTRY, outcome=telemetry.SKIPPED, reason="strategy_slot_lost_to", is_paper=True,
                           pair="BTC/USDT", now=datetime.now(timezone.utc), strategy_id=strategy_id, **kw)


def test_two_strategies_with_the_same_reason_on_the_same_pair_keep_one_row_each(db):
    st = make_settings(trade_telemetry=True)
    aid = new_account(st)
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        log(s, st, acct, "ema_cross_atr_v1")
        log(s, st, acct, "rsi_mr_v1")
    with session_scope() as s:
        rows = sorted(d.strategy_id for d in s.query(DecisionLog).filter(DecisionLog.account_id == aid).all())
    assert rows == ["ema_cross_atr_v1", "rsi_mr_v1"]


def test_the_same_strategy_repeating_the_same_decision_is_still_deduplicated(db):
    st = make_settings(trade_telemetry=True)
    aid = new_account(st)
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        log(s, st, acct, "rsi_mr_v1")
        log(s, st, acct, "rsi_mr_v1")
        log(s, st, acct, None)  # the default strategy id
        log(s, st, acct, None)
    with session_scope() as s:
        rows = sorted(d.strategy_id for d in s.query(DecisionLog).filter(DecisionLog.account_id == aid).all())
    assert rows == ["ema_cross_atr_v1", "rsi_mr_v1"]
