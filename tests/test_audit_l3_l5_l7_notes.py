"""Audit L3, L5 and L7 are noted in the docs and deliberately not changed in this PR."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_three_findings_are_noted_and_the_code_is_unchanged():
    doc = (ROOT / "docs/safety/multi_trade.md").read_text()
    for needle in ("**L3", "**L5", "**L7", "NOT changed"):
        assert needle in doc
    # L3: the existing risk code still passes a NaN through (a test pins the CURRENT behaviour so a change is deliberate)
    from mba_bot.risk import compute_stop_loss
    out = compute_stop_loss(entry_price=float("nan"), atr=1.0, side="long", atr_multiplier=2.0)
    assert out != out
    # L5: the downgrade is still a plain drop_column
    assert "op.drop_column" in (ROOT / "alembic/versions/a1d7c4e9b2f6_multi_trade_foundation.py").read_text()
