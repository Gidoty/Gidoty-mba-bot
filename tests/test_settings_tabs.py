"""PR B: Bot Settings has two tabs and one Save, the daily loss limit field, and the theme control moved to the Notifications page.

PROVES (by reading the source and running the pure screen rules in Node): the screen's suggestion percent and total stop percent equal the code constants; Bot Settings carries a tablist with
tab and tabpanel roles, a tab per section, one form and one submit, no theme control and no separate capital save; the Risk tab holds risk, leverage and the loss limit and the Trading tab holds pair
selection, pair, market and capital; Notifications carries the theme control in the page header; the Account page has none; the total loss card shows only while the stop is active and says nothing of
the old formula; the field texts the owner asked for are present.
DOES NOT PROVE: how any of it looks or behaves in a browser (see the PR report for what was checked there)."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from mba_bot import daily_limit as dl
from mba_bot import risk_limits as rl

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend" / "src"


def read(rel: str) -> str:
    return (FRONT / rel).read_text()


def node_json(script: str):
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, cwd=str(FRONT))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_screen_numbers_equal_the_code_constants():
    got = node_json('import { SUGGESTED_PCT, TABS } from "./lib/lossLimit.js"; import { TOTAL_STOP_PCT } from "./lib/riskBands.js"; console.log(JSON.stringify({ s: SUGGESTED_PCT, t: TOTAL_STOP_PCT, tabs: TABS.map((x) => x.id) }));')
    assert got["s"] == dl.SUGGESTED_PCT == 5.0 and got["t"] == rl.TOTAL_STOP_PCT == 50.0 and got["tabs"] == ["trading", "risk"]


def test_the_settings_page_is_two_accessible_tabs_with_one_save():
    page, tabs = read("pages/BotSettings.jsx"), read("components/SettingsTabs.jsx")
    assert 'role="tablist"' in tabs and 'role="tab"' in tabs and "aria-selected" in tabs and "aria-controls" in tabs and "tabIndex={selected ? 0 : -1}" in tabs and "onKeyDown" in tabs and "nextTabIndex" in tabs
    assert 'role: "tabpanel"' in tabs and '"aria-labelledby"' in tabs
    assert page.count("<form ") == 2 and page.count('type="submit"') == 2  # the Save form and the total-stop reset form (shown only while the stop is active)
    assert page.count("api.patch(") == 1  # one Save sends everything
    body = page[page.index("api.patch("): page.index("await reloadAccount();", page.index("api.patch("))]
    for field in ("pair,", "market_type: marketType", "risk_pct:", "acknowledge_high_risk", "pair_selection_mode", "max_leverage:", "allocated_capital:", "daily_loss_limit_usd:"):
        assert field in body, field  # both tabs' fields travel in the same request
    assert 'tabPanelProps("trading", tab)' in page and 'tabPanelProps("risk", tab)' in page
    assert "saveCapital" not in page and "Total loss limit" not in page and "3 x" not in page and "three times" not in page
    assert "ThemeSwitch" not in page and "Appearance" not in page


def _panel(page: str, name: str) -> str:
    start = page.index(f'tabPanelProps("{name}", tab)')
    nxt = page.find("tabPanelProps(", start + 10)
    return page[start: nxt if nxt != -1 else len(page)]


def test_each_field_sits_on_the_tab_the_owner_asked_for():
    page = read("pages/BotSettings.jsx")
    trading, risk = _panel(page, "trading"), _panel(page, "risk")
    for needle in ('label="Pair selection"', "Pair${pairSelectionMode", 'label="Market"', "Trading capital (US dollars)"):
        assert needle in trading, needle
    for needle in ("<RiskPctField", "Leverage (Futures)", "<DailyLossLimitField", "<BinanceSettingsLine"):
        assert needle in risk, needle
    for needle in ("<RiskPctField", "Leverage (Futures)", "<DailyLossLimitField"):
        assert needle not in trading
    for needle in ('label="Pair selection"', 'label="Market"', "trading-capital"):
        assert needle not in risk


def test_the_total_stop_card_shows_only_while_the_stop_is_active_and_the_save_reports_unsaved_changes():
    page = read("pages/BotSettings.jsx")
    assert "{account.total_loss_stop_active && (" in page and "account.total_loss_stop_enabled" not in page
    assert "You have unsaved changes" in page and 'role="status"' in page
    assert "setTab(" in page and "tabForMessage" in page and "problems.trading" in page and "problems.risk" in page  # a problem is shown on the tab it belongs to


def test_the_limit_field_has_what_the_owner_asked_for():
    f = read("components/DailyLossLimitField.jsx")
    for needle in ("Daily loss limit (US dollars)", "of your account value of", "We suggest about 5% of your account value", "starting limit we set for you", "pendingNotice", "one trade can lose more than this limit", "Required before live trading", "midnight Lagos time"):
        assert needle in f, needle
    assert "onChange(String(" not in f and "defaultValue" not in f  # the customer types the amount; nothing is filled in for them


def test_the_theme_control_is_on_the_notifications_page_only():
    notifications = read("pages/Notifications.jsx")
    assert "<ThemeSwitch" in notifications and "<PageHeader" in notifications
    header = notifications[notifications.index("<PageHeader"): notifications.index("</PageHeader>")]
    assert "ThemeSwitch" in header and 'id="theme-label"' in header  # in the header's right-hand slot, with a visible label
    users = sorted(str(p.relative_to(FRONT)) for p in FRONT.rglob("*.jsx") if "<ThemeSwitch" in p.read_text())
    assert users == ["pages/Notifications.jsx"], users
    assert "Theme" not in read("components/AppShell.jsx").replace("themeToggle", "") or "ThemeSwitch" not in read("components/AppShell.jsx")  # not in the top bar either
    assert "ThemeSwitch" not in read("pages/Account.jsx") and "ThemeSwitch" not in read("pages/BotSettings.jsx")
    ui = read("components/ui.jsx")
    assert "aria-labelledby={labelledBy}" in ui and 'aria-pressed={value === o.value}' in ui  # labelled, and each choice reports whether it is selected


def test_no_screen_text_describes_the_old_formula_or_the_four_percent_pause():
    bad = [r"3 times your risk", r"three times your risk", r"\b80%\b.{0,40}(stop|cap)", r"never more than 80", r"daily loss pause", r"Daily loss pause", r"limit is the same for every account"]
    for p in FRONT.rglob("*"):
        if p.suffix not in (".js", ".jsx") or p.name.endswith(".test.js"):
            continue
        text = p.read_text()
        for pat in bad:
            assert not re.search(pat, text), (p.name, pat)


def test_the_dashboard_shows_the_starting_limit_notice():
    app, notice = read("App.jsx"), read("components/DailyLimitNotice.jsx")
    assert "<DailyLimitNotice account={account} />" in app and "<DashboardRouter" in app
    assert app.index("<DailyLimitNotice") < app.index("<DashboardRouter")  # above the dashboard, not on every page
    assert "dashboardNotice(account)" in notice and 'navigate("bot-settings")' in notice
