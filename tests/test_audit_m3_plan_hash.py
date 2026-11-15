"""Audit M3: the plan hash of both backtest runners also pins the SHA-256 of rsi_backtest.py, donchian_backtest.py, indicators.py and strategy.py, so a
changed simulator, selection code, window code or indicator makes `confirm` refuse (it used to pin only the strategy file)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mba_bot import donchian_backtest as DB
from mba_bot import rsi_backtest as RB

PINNED = ("rsi_backtest.py", "donchian_backtest.py", "indicators.py", "strategy.py")


@pytest.mark.parametrize("module,own", [(RB, "rsi_mr.py"), (DB, "donchian.py")], ids=["rsi", "donchian"])
@pytest.mark.parametrize("which", range(len(PINNED) + 1))
def test_changing_any_pinned_file_changes_the_plan_hash(monkeypatch, module, own, which):
    name = (PINNED + (own,))[which]
    base = module.plan_hash()
    real = RB._sha

    def patched(path):
        return "changed" if Path(path).name == name else real(path)

    monkeypatch.setattr(RB, "_sha", patched)
    monkeypatch.setattr(DB, "_sha", patched, raising=False)
    assert module.plan_hash() != base, name


@pytest.mark.parametrize("module", [RB, DB], ids=["rsi", "donchian"])
def test_an_unchanged_tree_gives_a_stable_hash(module):
    assert module.plan_hash() == module.plan_hash()
