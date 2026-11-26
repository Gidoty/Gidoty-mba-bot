"""Runs the password reset and email confirmation helpers' JavaScript tests (frontend/src/lib/recovery.test.js) with Node's built-in runner. Skipped only when Node is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_recovery_helpers():
    result = subprocess.run(["node", "--test", str(LIB / "recovery.test.js")], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "# fail 0" in result.stdout and "# pass 4" in result.stdout
