"""Audit M7: the documented deploy order is: check no live trade is open, STOP the service, BACK UP, MIGRATE, DEPLOY the code, START the service. Either other order
leaves a window (new schema under old code, or new code on an old schema). Every owner-steps section that mentions a migration or a deploy says the same."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs" / "safety"


def owner_steps(name):
    """The numbered steps of the owner-steps section (the paragraph before them explains why, and may name the actions in another order)."""
    text = (DOCS / name).read_text()
    section = text[text.index("## Owner steps"):]
    lines = [l for l in section.splitlines() if re.match(r"^\d+\. ", l)]
    return "\n".join(lines) if lines else section


def positions(text, needles):
    out = []
    for n in needles:
        m = re.search(n, text, re.I)
        assert m, f"{n!r} missing"
        out.append(m.start())
    return out


@pytest.mark.parametrize("name", ["multi_trade.md", "rsi_mr_v1.md", "donchian_breakout_v1.md", "strategy_guards.md"])
def test_the_owner_steps_use_the_safe_order(name):
    text = owner_steps(name)
    p = positions(text, [r"restart_gate", r"stop the service", r"back up", r"alembic upgrade head|migrate", r"deploy the code", r"start the service"])
    assert p == sorted(p), (name, p)


def test_multi_trade_lists_the_whole_migration_chain_and_the_reason():
    text = (DOCS / "multi_trade.md").read_text()
    text = text[text.index("## Owner steps"):]
    for rev in ("a1d7c4e9b2f6", "e7a2c9d4b1f8", "f3c8a1d6e2b9"):
        assert rev in text
    assert "old code" in text and "new schema" in text  # why migrate-then-deploy with the service running is not enough
    assert "alembic upgrade head" in text


def test_the_old_order_is_gone():
    assert "Back up the database. Run migration `a1d7c4e9b2f6`. Then deploy the code" not in (DOCS / "multi_trade.md").read_text()
