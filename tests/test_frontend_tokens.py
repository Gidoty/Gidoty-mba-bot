"""The design tokens must keep passing the WCAG contrast check in both themes.

Pure file parsing: no browser, no network. Run the same tool on a built CSS file to check what ships:
    python frontend/tools/contrast_check.py frontend/dist/assets/index-*.css
"""
import importlib.util
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
spec = importlib.util.spec_from_file_location("contrast_check", FRONTEND / "tools" / "contrast_check.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

TOKENS = (FRONTEND / "src" / "tokens.css").read_text()


def test_every_pair_passes_in_both_themes():
    rows, fails = cc.check(TOKENS)
    bad = [r for r in rows if not r[1]]
    assert fails == 0, bad
    assert {r[0] for r in rows} == {"light", "dark"}
    assert len(rows) >= 60


def test_known_bad_value_is_caught():
    # The plain amber on white was the original focus-ring and chart-line failure (2.41:1).
    broken = TOKENS.replace("--accent-line: #C26A08;", "--accent-line: #F0901E;", 1)
    rows, fails = cc.check(broken)
    assert fails >= 1
    assert any(not ok and "accent-line" in text for _, ok, _, _, text in rows)


def test_media_fallback_must_match_the_dark_block():
    broken = TOKENS.replace("--pos: #4cd18c;", "--pos: #4cd18d;", 1)  # first occurrence is the explicit dark block
    rows, fails = cc.check(broken)
    assert any("media fallback" in text for _, ok, _, _, text in rows if not ok)


def test_theme_module_never_throws_without_storage():
    src = (FRONTEND / "src" / "theme.js").read_text()
    assert src.count("try {") >= 2 and src.count("catch") >= 2  # storage reads and writes are guarded
