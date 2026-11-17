"""The plain definition of risk per trade (info icon next to the field, How-to, Help), the corrected Bot Settings tooltip, and the Auto Select sentence on the "Binance for <pair>" line.

PROVES: the definition, its 100 USDT / 2% example and the risk-versus-leverage sentence are one wording used by the dialog, the How-to guide and the Help page; the example is TRUE in the code (position sizing
loses 2 USDT at the stop and the trade is worth more than 2 USDT); the Bot Settings tooltip says exactly what the router does (only pair, market and pair selection mode are blocked with a trade open; the
tooltip is derived from the code's own guard); the Auto Select sentence is in the doc, the screen constant and the component.
DOES NOT PROVE: how the text reads to a first-time user, or the dialog in a browser."""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from mba_bot.risk import compute_position_size

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend" / "src"


def node_json(script: str):
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, cwd=str(FRONT))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def js():
    return node_json(
        """
import * as g from "./riskGuideText.js";
import { howToSections } from "./howTo.js";
import { AUTO_SELECT_BINANCE_NOTE } from "./leverageHelp.js";
const steps = Object.fromEntries(howToSections("1.2.3.4").flatMap((s) => s.steps));
console.log(JSON.stringify({ g: { d: g.RISK_DEFINITION, n: g.RISK_NOT, e: g.RISK_EXAMPLE, v: g.RISK_VS_LEVERAGE, w: g.RISK_WORSE_FILL }, help: g.RISK_PER_TRADE_HELP, topics: g.RISK_HELP_TOPICS, steps, auto: AUTO_SELECT_BINANCE_NOTE }));
"""
    )


# ------------------------------------------------------------------ the definition
def test_the_definition_says_what_the_code_does(js):
    g = js["g"]
    assert "share of your trading capital the bot is willing to lose on one trade if its stop-loss is hit" in g["d"]
    assert "not the amount put into the trade" in g["n"] and "not the size of the trade" in g["n"]
    assert "100 USDT" in g["e"] and "2%" in g["e"] and "about 2 USDT" in g["e"] and "can be worth more than 2 USDT" in g["e"]
    assert "risk sets how much a stop-loss hit may lose" in g["v"] and "leverage setting caps how large the trade can be compared with your trading capital" in g["v"]
    assert "worse price than planned, the loss can be larger" in g["w"]


def test_one_wording_in_the_dialog_the_guide_and_the_help_page(js):
    g = js["g"]
    dialog = " ".join(b["text"] for b in js["help"]["blocks"])
    for sentence in (g["d"], g["n"], g["e"], g["v"], g["w"]):
        assert sentence in dialog
    assert g["d"] in js["steps"]["Risk per trade"] and g["n"] in js["steps"]["Risk per trade"] and g["e"] in js["steps"]["Risk per trade"]
    assert g["w"] in js["steps"]["Choosing your risk"] and g["v"] in js["steps"]["Risk and leverage compared"]
    first, second = js["topics"][0]["body"], js["topics"][1]["body"]
    for sentence in (g["d"], g["n"], g["e"], g["w"]):
        assert sentence in first
    assert g["v"] in second


def test_the_example_is_true_in_the_code():
    """100 USDT, 2% risk: the bot sizes the trade so a stop-loss hit loses 2 USDT, and the trade is worth more than 2 USDT."""
    entry, stop = 100.0, 95.0
    size = compute_position_size(account_balance=100.0, risk_pct=2.0, entry_price=entry, stop_price=stop, max_risk_pct=40.0)
    assert abs(size.risk_amount - 2.0) < 1e-9
    assert abs(size.quantity * (entry - stop) - 2.0) < 1e-9  # the loss if the stop is hit
    assert size.quantity * entry > 2.0  # the trade is worth far more than the amount at risk (here 40 USDT)
    wider = compute_position_size(account_balance=100.0, risk_pct=2.0, entry_price=entry, stop_price=50.0, max_risk_pct=40.0)
    assert abs(wider.quantity * (entry - 50.0) - 2.0) < 1e-9  # the same 2 USDT at risk with a different trade size: risk is not the size


def test_the_field_has_the_info_icon_and_a_real_label():
    field = (FRONT / "components" / "RiskPctField.jsx").read_text()
    assert "<InfoHelp" in field and "RISK_PER_TRADE_HELP" in field and "htmlFor={inputId}" in field and "useId()" in field and "aria-describedby" in field
    assert "not the size of the trade" in field
    for page in ("pages/BotSettings.jsx", "pages/Onboarding.jsx"):
        assert "<RiskPctField" in (FRONT / page).read_text(), page  # both screens get the icon through the one field


# ------------------------------------------------------------------ the tooltip tells the truth about what is blocked
TOOLTIP = ("You can change your risk per trade, leverage and trading capital at any time; they apply to new trades only. "
           "Your pair, market and pair selection mode cannot be changed while a trade is open, so nothing gets left unmanaged.")


def _update_account():
    tree = ast.parse((ROOT / "src/mba_bot/api/routers/account.py").read_text())
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "update_account")


def _guard_facts():
    fn = _update_account()
    blocked_fields: set[str] = set()
    flags: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("changing_"):
            flags[node.targets[0].id] = {a.attr for a in ast.walk(node.value) if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "body"}
    guarded_ifs = []
    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "_require_no_open_trade":
                    guarded_ifs.append(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_require_no_open_trade":
            calls.append(node)
    return flags, guarded_ifs, calls


def test_the_open_trade_guard_is_called_once_and_only_for_pair_market_and_pair_selection_mode():
    flags, guarded_ifs, calls = _guard_facts()
    assert len(calls) == 1
    # the guard sits inside `if changing_pair or changing_market or changing_selection_mode:` and nowhere else in the handler
    outer = [n for n in guarded_ifs if isinstance(n.test, ast.BoolOp)]
    assert len(outer) == 1
    names = {v.id for v in outer[0].test.values}
    assert names == {"changing_pair", "changing_market", "changing_selection_mode"}
    blocked = set().union(*(flags[n] for n in names))
    assert blocked == {"pair", "market_type", "pair_selection_mode"}
    # the call is not reachable from the risk, leverage or capital branches
    fn = _update_account()
    for field in ("risk_pct", "max_leverage", "allocated_capital"):
        branch = next(n for n in fn.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Attribute) and n.test.left.attr == field)
        assert not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_require_no_open_trade" for c in ast.walk(branch)), field


def test_the_tooltip_is_the_sentence_and_follows_the_code():
    settings = (FRONT / "pages" / "BotSettings.jsx").read_text()
    assert f'<HelpTip text="{TOOLTIP}" />' in settings
    flags, _, _ = _guard_facts()
    blocked = {"pair", "market_type", "pair_selection_mode"}
    editable = {"risk_pct": "risk per trade", "max_leverage": "leverage", "allocated_capital": "trading capital"}
    from mba_bot.api.schemas import AccountUpdateRequest

    for field, words in editable.items():
        assert field in AccountUpdateRequest.model_fields and field not in blocked and words in TOOLTIP.split(";")[0]
    for words in ("pair", "market", "pair selection mode"):
        assert words in TOOLTIP.split(";")[1]
    old = "You can change your pair, market, risk, or pair selection mode at any time"
    for path in list(FRONT.rglob("*.js")) + list(FRONT.rglob("*.jsx")) + [ROOT / "README.md"] + list((ROOT / "docs").rglob("*.md")):
        if path.name.startswith("test_") or path.suffix == ".test.js" or path.name.endswith("test.js"):
            continue
        text = path.read_text(errors="ignore")
        assert old not in text, path
        assert not re.search(r"\brisk\b[^.]{0,60}(blocked|cannot be changed|can't be changed)[^.]{0,40}(trade is open|open trade)", text, re.I), path


def test_other_screens_do_not_repeat_the_mistake():
    guide = (FRONT / "howTo.js").read_text()
    assert "Pair, market and pair selection mode cannot be changed while a trade is open" in guide and "Risk per trade and trading capital can be changed at any time" in guide
    help_page = (FRONT / "pages" / "Help.jsx").read_text() + (FRONT / "pages" / "Onboarding.jsx").read_text()
    assert not re.search(r"(blocked|cannot be changed)[^.]{0,40}while a trade is open", help_page, re.I)


# ------------------------------------------------------------------ the Auto Select sentence
def test_the_auto_select_sentence_is_in_the_doc_the_constant_and_the_component(js):
    note = js["auto"]
    assert note == "Auto Select is on: this line shows the pair saved on your account. The bot reads Binance's settings for each pair just before it trades it."
    assert note in (ROOT / "docs/customer/leverage_help.md").read_text()
    line = (FRONT / "components" / "BinanceSettingsLine.jsx").read_text()
    assert re.search(r'\{autoSelect && state\.status !== "not_applicable" && <p[^>]*>\{AUTO_SELECT_BINANCE_NOTE\}</p>\}', line)  # shown only with Auto Select, and not for practice or Spot accounts
    assert 'autoSelect={account.pair_selection_mode === "auto"}' in (FRONT / "pages" / "BotSettings.jsx").read_text()
    assert "UNVERIFIED" not in note
