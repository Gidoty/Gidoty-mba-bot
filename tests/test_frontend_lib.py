"""Runs the frontend's pure-JavaScript tests (statistics and number formatting) with Node's built-in runner.

These cover the figures the redesigned pages show: win rate, profit factor, drawdown, daily and monthly
buckets. Skipped only when Node is not installed, with the reason shown.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_statistics_and_formatting():
    result = subprocess.run(
        ["node", "--test", str(LIB / "stats.test.js"), str(LIB / "position.test.js"), str(LIB / "tradeStatus.test.js"), str(LIB / "history.test.js"), str(LIB.parent / "howTo.test.js")], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "# fail 0" in result.stdout
    assert "# pass 36" in result.stdout
