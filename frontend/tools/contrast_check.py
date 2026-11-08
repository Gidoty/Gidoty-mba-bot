"""WCAG 2.x contrast check for the design tokens.

Usage:  python frontend/tools/contrast_check.py [css file ...]
Default file: frontend/src/tokens.css. Pass a built file (frontend/dist/assets/*.css) to check what ships.
Reads the light tokens from the first :root block and the dark tokens from :root[data-theme=dark],
checks that the no-script media fallback matches the dark block, then tests the pairs below.
Text needs 4.5:1; chart marks, the focus ring and input or button borders need 3:1 (WCAG 1.4.11).
Exit code 1 if any pair fails. Standard library only.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (foreground token, background token, minimum ratio, what it protects)
PAIRS = [
    ("text", "bg", 4.5, "body text on page"),
    ("text", "surface", 4.5, "body text on card"),
    ("text", "surface-2", 4.5, "text on raised or hover"),
    ("muted", "bg", 4.5, "muted text on page"),
    ("muted", "surface", 4.5, "muted text on card (labels, chart axis, placeholder)"),
    ("muted", "surface-2", 4.5, "muted text on raised or hover"),
    ("accent-text", "surface", 4.5, "link on card"),
    ("accent-text", "bg", 4.5, "link on page"),
    ("accent-text", "accent-soft", 4.5, "selected segment"),
    ("pos", "surface", 4.5, "gain text on card"),
    ("pos", "pos-soft", 4.5, "Win pill"),
    ("pos", "surface-2", 4.5, "gain text on hover row"),
    ("neg", "surface", 4.5, "loss text on card"),
    ("neg", "neg-soft", 4.5, "Loss pill"),
    ("neg", "surface-2", 4.5, "loss text on hover row"),
    ("warn", "warn-soft", 4.5, "caution pill"),
    ("text", "warn-soft", 4.5, "notice text"),
    ("info", "info-soft", 4.5, "info pill"),
    ("text", "info-soft", 4.5, "info notice text"),
    ("accent-ink", "accent", 4.5, "primary button label"),
    ("nav-text", "nav", 4.5, "rail and tab text"),
    ("nav-text", "nav-active", 4.5, "rail hover text"),
    ("pos-bar", "surface", 3.0, "gain bar vs card (graphic)"),
    ("neg-bar", "surface", 3.0, "loss bar vs card (graphic)"),
    ("accent-line", "surface", 3.0, "equity line and focus ring vs card (graphic)"),
    ("accent-line", "bg", 3.0, "focus ring vs page"),
    ("accent-line", "surface-2", 3.0, "focus ring vs raised"),
    ("line-strong", "surface", 3.0, "input, button and segmented-control border (WCAG 1.4.11)"),
    ("line-strong", "bg", 3.0, "button border on page"),
    ("muted", "surface", 3.0, "zero baseline vs card (graphic)"),
]
# Fixed colours the shell uses on the rail, which is dark in both themes.
FIXED = [("#F6AC4D", "nav", 4.5, "active tab colour on rail"), ("#ffffff", "nav-active", 4.5, "active rail item text")]


def _hex(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.lower()


def _tokens(body):
    return {k: _hex(v) for k, v in re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{3,6})\b", body)}


def _block(css, selector_pat):
    m = re.search(selector_pat + r"\s*\{([^}]*)\}", css)
    if not m:
        raise ValueError("block not found: " + selector_pat)
    return _tokens(m.group(1))


def parse(css):
    light = _block(css, r"(?<![\w\]\)])\:root(?!\[|\:)")
    dark_over = _block(css, r"\:root\[data-theme=[\"']?dark[\"']?\]")
    dark = dict(light)
    dark.update(dark_over)
    fallback = _block(css, r"\:root\:not\(\[data-theme\]\)")
    return light, dark, dark_over, fallback


def luminance(h):
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check(css):
    """Return (rows, failures). Each row: (theme, ok, ratio, minimum, text)."""
    light, dark, dark_over, fallback = parse(css)
    rows, fails = [], 0
    if {k: v for k, v in fallback.items()} != dark_over:
        rows.append(("both", False, 0.0, 0.0, "media fallback differs from the explicit dark block"))
        fails += 1
    for name, t in (("light", light), ("dark", dark)):
        for fg, bg, need, what in PAIRS:
            r = ratio(t[fg], t[bg])
            ok = r >= need
            fails += not ok
            rows.append((name, ok, r, need, f"{fg} on {bg}: {what}"))
        for fg, bg, need, what in FIXED:
            r = ratio(fg, t[bg])
            ok = r >= need
            fails += not ok
            rows.append((name, ok, r, need, f"{fg} on {bg}: {what}"))
    return rows, fails


def main(argv):
    paths = [Path(a) for a in argv] or [ROOT / "src" / "tokens.css"]
    total = 0
    for p in paths:
        rows, fails = check(p.read_text())
        print(f"== {p}")
        for theme, ok, r, need, text in rows:
            print(f"{'PASS' if ok else 'FAIL'} {theme:5} {r:5.2f} (need {need}) {text}")
        print(f"{len(rows)} checks, {fails} failures")
        total += fails
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
