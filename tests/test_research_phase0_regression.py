"""The Phase 1 engine, with every policy at its default, must reproduce the Phase 0 engine bit for bit.

The golden file was produced by the Phase 0 engine (commit c5a92b3) on a fixed synthetic store. Without this guard the
Phase 1 refactor could quietly change what 'the baseline' means."""
import json
from pathlib import Path

import research_golden as g

GOLDEN = Path(__file__).parent / "golden" / "phase0_engine.json"


def test_default_policy_reproduces_the_phase_0_engine_exactly(tmp_path):
    g.make_store(tmp_path / "store")
    got = g.run_all(tmp_path / "store")
    gold = json.loads(GOLDEN.read_text())
    assert set(got) == set(gold)
    for key, expected in gold.items():
        assert got[key] == expected, key
