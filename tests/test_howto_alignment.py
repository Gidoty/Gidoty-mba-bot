"""The words the app shows about risk, leverage and the safety limits must match the code as deployed: the How-to guide, the Help page, the Landing page and the screens' own constants.

PROVES: the numbers in frontend/src/lib/riskBands.js (and so in the guide text built from them) equal the constants in risk_limits.py and leverage.py; the guide, Help and Landing never say 2 percent is the
maximum; the acknowledgment records, texts and screens are consistent with a ceiling of 40; the acknowledgment document types fit the column and are the only ones legal.py knows beyond the three legal documents.
DOES NOT PROVE: that a screen reads well to a first-time user, or anything about Binance."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from mba_bot import leverage as lv
from mba_bot import legal
from mba_bot import risk_limits as rl
from mba_bot.models import LegalAcceptance

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
import * as b from "./lib/riskBands.js";
import { howToSections } from "./howTo.js";
import { RISK_HELP_TOPICS } from "./riskGuideText.js";
const consts = Object.fromEntries(Object.entries(b).filter(([k, v]) => typeof v === "number"));
console.log(JSON.stringify({ consts, ack: b.ACK_TEXT, steps: howToSections("1.2.3.4").flatMap((s) => s.steps.map(([t, x]) => [t, x])), help: RISK_HELP_TOPICS }));
"""
    )


def test_the_screen_constants_equal_the_code_constants(js):
    c = js["consts"]
    assert c["RISK_MIN"] == rl.RISK_MIN and c["RISK_CEILING"] == rl.RISK_CEILING == 40.0
    assert c["RECOMMENDED_MAX"] == rl.RISK_RECOMMENDED_MAX and c["VERY_HIGH_FROM"] == rl.RISK_VERY_HIGH_FROM
    assert c["TOTAL_STOP_MIN_PCT"] == rl.TOTAL_STOP_MIN_PCT and c["TOTAL_STOP_MULTIPLE"] == rl.TOTAL_STOP_MULTIPLE and c["TOTAL_STOP_CAP_PCT"] == rl.TOTAL_STOP_CAP_PCT
    assert (c["LEVERAGE_MIN"], c["LEVERAGE_MAX"], c["LEVERAGE_DEFAULT"]) == (lv.LEVERAGE_MIN, lv.LEVERAGE_MAX, lv.LEVERAGE_DEFAULT)


def _step(js, title):
    return next(b for t, b in js["steps"] if t == title)


def test_the_guide_numbers_match_the_code(js):
    risk = _step(js, "Risk per trade")
    assert f"from {rl.RISK_MIN:g}% up to {rl.RISK_CEILING:g}% at most" in risk
    assert f"We recommend {rl.RISK_RECOMMENDED_MAX:g}% or less" in risk and f"Above {rl.RISK_RECOMMENDED_MAX:g}%" in risk and f"From {rl.RISK_VERY_HIGH_FROM:g}%" in risk
    lev = _step(js, "Leverage on Futures")
    assert f"{lv.LEVERAGE_MIN}x to {lv.LEVERAGE_MAX}x, {lv.LEVERAGE_DEFAULT}x until you change it" in lev
    total = _step(js, "Total loss stop")
    assert f"{rl.TOTAL_STOP_MIN_PCT:g}% below" in total and f"{rl.TOTAL_STOP_MULTIPLE:g} times your risk per trade" in total and f"never more than {rl.TOTAL_STOP_CAP_PCT:g}%" in total


def test_the_total_loss_threshold_the_text_describes_is_the_one_the_code_computes():
    # "10% or 3 times the risk per trade if that is more, never more than 80%"
    for risk in (0.1, 1, 2, 3.33, 5, 10, 20, 26.67, 40):
        assert rl.total_loss_threshold(risk) == min(80.0, max(10.0, 3.0 * risk))


def test_nothing_says_two_percent_is_the_maximum(js):
    texts = [b for _, b in js["steps"]] + [t["body"] for t in js["help"]] + [(FRONT / "pages" / "Landing.jsx").read_text(), (FRONT / "pages" / "Onboarding.jsx").read_text(), (FRONT / "pages" / "BotSettings.jsx").read_text(),
                                                                         (FRONT / "components" / "RiskPctField.jsx").read_text()]
    old = [r"at most 2%", r"maximum[^.]{0,40}\b2%", r"capped at 2%", r"between 0\.1% and 2%", r"0\.1% to 2%", r"2% maximum", r"up to 2%", r"max=\"2\"", r"hard cap"]
    for text in texts:
        for pattern in old:
            assert not re.search(pattern, text, re.I), (pattern, text[:80])
    for doc in ("README.md", "ARCHITECTURE.md"):
        assert not re.search(r"1-2%", (ROOT / doc).read_text()), doc


def test_every_screen_takes_the_limit_from_the_server_not_from_a_constant():
    settings = (FRONT / "pages" / "BotSettings.jsx").read_text()
    onboarding = (FRONT / "pages" / "Onboarding.jsx").read_text()
    assert "account.risk_pct_limit" in settings and "/account/risk-limits" in onboarding
    field = (FRONT / "components" / "RiskPctField.jsx").read_text()
    assert "max={limit}" in field and "up to {limit}%" in field


# ------------------------------------------------------------------ acknowledgments and a ceiling of 40
def test_the_acknowledgment_records_fit_the_column_and_the_ceiling():
    column = LegalAcceptance.__table__.c.document_type.type.length
    version_column = LegalAcceptance.__table__.c.version.type.length
    assert len(rl.ACK_HIGH) <= column and len(rl.ACK_VERY_HIGH) <= column and len(rl.ACK_VERSION) <= version_column
    assert set(legal.DOCUMENT_TYPES) == {"terms", "privacy", "risk_disclosure", rl.ACK_HIGH, rl.ACK_VERY_HIGH}
    assert rl.required_acknowledgments(rl.RISK_CEILING) == (rl.ACK_HIGH, rl.ACK_VERY_HIGH) and rl.band(rl.RISK_CEILING) == rl.BAND_VERY_HIGH
    assert rl.validate_range(rl.RISK_CEILING, rl.clamp_limit(1000)) is None and rl.validate_range(rl.RISK_CEILING + 0.01, rl.clamp_limit(1000)) is not None


def test_the_acknowledgment_texts_match_the_bands_and_say_nothing_about_a_smaller_ceiling(js):
    high, very = js["ack"]["high"], js["ack"]["very_high"]
    assert f"above {rl.RISK_RECOMMENDED_MAX:g}%" in high and f"{rl.RISK_VERY_HIGH_FROM:g}% or more" in very and f"{rl.RISK_RECOMMENDED_MAX:g}% or less is recommended" in very
    for text in (high, very):
        assert not re.search(r"at most|maximum|\bcap\b", text, re.I) and "guarantee" not in text.lower()
    assert js["consts"]["RECOMMENDED_MAX"] == rl.RISK_RECOMMENDED_MAX and js["consts"]["VERY_HIGH_FROM"] == rl.RISK_VERY_HIGH_FROM


def test_the_acknowledgments_are_not_tied_to_the_legal_page_versions():
    """A Terms or Risk Disclosure version bump must not change what the risk acknowledgments mean: they have their own version, and the legal gate reads only terms and privacy."""
    gate = (ROOT / "src" / "mba_bot" / "legal.py").read_text()
    body = gate[gate.index("def has_current_terms_and_privacy"):gate.index("def record_acceptance")]
    assert "risk_ack" not in body and "risk_disclosure" not in body


# ------------------------------------------------------------------ the legal draft exists, is marked, and the live pages are untouched
def test_the_legal_draft_is_marked_and_has_the_required_topics():
    text = (ROOT / "docs" / "legal" / "DRAFT_risk_disclosure_changes.md").read_text()
    assert text.splitlines()[0].startswith("# DRAFT") and "not legal advice" in text.lower() and "for review by the owner and his lawyer" in text.lower()
    for topic in ("risk per trade", "total loss stop", "size cap", "practice", "no guarantee", "isolated", "daily loss", "version bump"):
        assert topic in text.lower(), topic
    for bad in ("MBA_BOT_FINAL_QA_QC_AUDIT", "handoff", "stack-audit"):
        assert bad.lower() not in text.lower(), bad
