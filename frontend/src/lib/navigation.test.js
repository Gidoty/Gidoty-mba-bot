import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import test from "node:test";

import { NAV_ITEMS } from "../constants.js";
import { ICON_PATHS, ICON_STROKE } from "./navIconPaths.js";
import { BOTTOM_TABS, HOME_ROUTE, LEGAL_LINKS, MORE_GROUPS, activeTab, canonicalRoute, isMoreRoute, moreGroups, sidebarItems } from "./navigation.js";

const src = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const codeOf = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "").replace(/ \/\/ .*$/gm, "");

test("the phone's bottom bar has exactly six items in this order", () => {
  assert.deepEqual(BOTTOM_TABS.map((t) => t.label), ["Home", "Positions", "History", "Subscriptions", "Performance", "More"]);
  assert.deepEqual(BOTTOM_TABS.map((t) => t.route), ["home", "positions", "trade-history", "subscriptions", "performance", "more"]);
});

test("Dashboard is called Home everywhere in the navigation", () => {
  assert.equal(NAV_ITEMS.find((i) => i.route === "home").label, "Home");
  assert.ok(!NAV_ITEMS.some((i) => /dashboard/i.test(i.label) || i.route === "dashboard"));
  assert.ok(!BOTTOM_TABS.some((t) => /dashboard/i.test(t.label)));
  assert.equal(HOME_ROUTE, "home");
});

test("the old dashboard link still opens Home", () => {
  assert.equal(canonicalRoute("dashboard"), "home");
  assert.equal(canonicalRoute("positions"), "positions");
  assert.equal(activeTab("dashboard"), "home");
  assert.ok(src("../App.jsx").includes("canonicalRoute(useHashRoute())"));
});

test("More lists every page that has no tab, grouped, and nothing that exists today is dropped", () => {
  assert.deepEqual(MORE_GROUPS.map((g) => g.title), ["Trading", "Alerts and help", "Account"]);
  assert.deepEqual(MORE_GROUPS.flatMap((g) => g.items.map((i) => i.label)), ["Bot Settings", "Exchange Connection", "Testnet / Practice", "Notifications", "Help & Guide", "Account"]);
  const tabRoutes = new Set(BOTTOM_TABS.map((t) => t.route));
  const inMore = new Set(MORE_GROUPS.flatMap((g) => g.items.map((i) => i.route)));
  for (const item of NAV_ITEMS) assert.ok(tabRoutes.has(item.route) || inMore.has(item.route), `${item.route} must be a tab or inside More`);
  assert.deepEqual(moreGroups(true).at(-1), { title: "Admin", items: [{ route: "admin", label: "Admin" }] }); // the admin page joins for an admin
  assert.equal(moreGroups(false).length, 3);
});

test("the desktop sidebar has the same names in the same order, with no legal links", () => {
  assert.deepEqual(sidebarItems().map((i) => i.label), ["Home", "Positions", "History", "Subscriptions", "Performance", "Bot Settings", "Exchange Connection", "Testnet / Practice", "Notifications", "Help & Guide", "Account"]);
  assert.deepEqual(NAV_ITEMS.map((i) => i.label), sidebarItems().map((i) => i.label));
  const shell = codeOf(src("../components/AppShell.jsx"));
  assert.ok(!/terms|privacy|risk-disclosure|LEGAL/i.test(shell), "no legal links in the sidebar or the bottom bar");
});

test("the More item is active on More and on every page inside it, and a page with a tab keeps its own tab", () => {
  for (const g of MORE_GROUPS) for (const i of g.items) assert.equal(activeTab(i.route), "more", i.route);
  assert.equal(activeTab("more"), "more");
  assert.equal(activeTab("admin"), "more");
  assert.equal(activeTab("positions"), "positions");
  assert.equal(activeTab("subscriptions"), "subscriptions");
  assert.equal(activeTab("nonsense"), "home");
  assert.equal(isMoreRoute("home"), false);
});

test("Terms, Privacy and the Risk Disclosure are three rows at the bottom of Account, and no footer shows them inside the signed-in app", () => {
  assert.deepEqual(LEGAL_LINKS.map((l) => l.label), ["Terms of Service", "Privacy Policy", "Trading Risk Disclosure"]);
  const account = src("../pages/Account.jsx");
  assert.ok(account.includes('aria-label="Legal"') && account.includes("LEGAL_LINKS.map"));
  assert.ok(account.indexOf('aria-label="Legal"') > account.indexOf("Log out"), "Legal is below Log out, at the bottom");
  assert.ok(account.includes("onClick={onLogout}"), "Log out stays inside Account");
  const app = src("../App.jsx");
  assert.ok(app.includes("{!inApp && <Footer"), "the footer is not drawn while the signed-in app is on screen");
  assert.ok(!codeOf(src("../pages/More.jsx")).includes("onLogout"), "Log out is not a row in More");
});

test("the bottom bar is built as asked", () => {
  const shell = src("../components/AppShell.jsx");
  assert.equal(shell.split('aria-label="Main"').length - 1, 2, "the sidebar and the bottom bar are both labelled Main");
  assert.equal(shell.split('aria-current={active ? "page" : undefined}').length - 1, 2, "both mark the active item");
  assert.ok(shell.includes("min-h-[56px]") && shell.includes("min-w-[44px]") && shell.includes("pb-[env(safe-area-inset-bottom)]") && shell.includes("text-[11px]"));
  assert.ok(shell.includes("fixed inset-x-0 bottom-0"));
  assert.ok(shell.includes("whitespace-nowrap") && !/truncate|text-ellipsis|overflow-hidden/.test(shell.slice(shell.indexOf("function TabLink"))), "a label is never cut off");
  assert.ok(shell.includes("BOTTOM_TABS.map"));
  assert.ok(shell.includes('text-brand-text') && shell.includes('text-muted'), "active in the brand token, inactive in the muted token");
  assert.ok(shell.includes("pb-[calc(5.5rem+env(safe-area-inset-bottom))]"), "page content never hides behind the bar");
});

test("one icon set: outline only, one stroke weight, a 24 px viewBox", () => {
  const icons = src("../components/navIcons.jsx");
  assert.equal(ICON_STROKE, 1.75);
  assert.ok(!/(^|\s)fill="(?!none)/.test(src("./navIconPaths.js")));
  assert.ok(icons.includes('viewBox="0 0 24 24"') && icons.includes('fill="none"') && icons.includes("strokeWidth={ICON_STROKE}"));
  assert.ok(!/fill="(?!none)/.test(icons));
  for (const t of BOTTOM_TABS) assert.ok(ICON_PATHS[t.route], `an icon for ${t.route}`);
  for (const g of MORE_GROUPS) for (const i of g.items) assert.ok(ICON_PATHS[i.route], `an icon for ${i.route}`);
  assert.ok(ICON_PATHS.chevron);
});

test("More is a page whose rows are at least 56 px tall with an icon, the label and a chevron", () => {
  const more = src("../pages/More.jsx");
  assert.ok(more.includes("min-h-[56px]") && more.includes('route="chevron"') && more.includes("<NavIcon route={item.route}"));
  assert.ok(src("../App.jsx").includes("more: More,"));
});

test("the Home screen has no title: it starts with its first card", () => {
  for (const f of ["../pages/dashboard/TestnetDashboard.jsx", "../pages/dashboard/LiveDashboard.jsx"]) {
    const code = codeOf(src(f));
    assert.ok(!code.includes("<PageHeader"), f);
    assert.ok(!/title="(Dashboard|Home)"/.test(code), f);
    assert.ok(code.includes('<div className="grid gap-4">\n      <Card'), `${f}: the first thing is a card`);
  }
});

test("no screen text still says Dashboard (the legal documents are left exactly as they are)", () => {
  const files = [];
  const walk = (dir) => {
    for (const name of readdirSync(new URL(dir, import.meta.url))) {
      const rel = `${dir}${name}`;
      if (statSync(new URL(rel, import.meta.url)).isDirectory()) walk(`${rel}/`);
      else if (/\.(jsx?|mjs)$/.test(name) && !/\.test\.js$/.test(name)) files.push(rel);
    }
  };
  walk("../");
  const bad = [];
  for (const f of files) {
    if (/legalContent\.js$|pages\/Admin\.jsx$|lib\/admin(Actions)?\.js$/.test(f)) continue; // legal text is not changed; the admin dashboard is a different thing
    const code = codeOf(src(f));
    for (const m of code.matchAll(/["'`][^"'`\n]*\bDashboard\b[^"'`\n]*["'`]/g)) bad.push(`${f}: ${m[0].slice(0, 80)}`);
    for (const m of code.matchAll(/>[^<>{}\n]*\bDashboard\b[^<>{}\n]*</g)) bad.push(`${f}: ${m[0].slice(0, 80)}`);
  }
  assert.deepEqual(bad, []);
});

test("the guide and the links that point to Home say Home", () => {
  const how = src("../howTo.js");
  assert.ok(how.includes("Watch it on Home.") && how.includes("On Home, follow") && how.includes('["Home", "Exchange balance'));
  assert.ok(src("../pages/ExchangeConnection.jsx").includes('navigate("home")') && src("../pages/Practice.jsx").includes('navigate("home")'));
  assert.ok(src("../pages/ExchangeConnection.jsx").includes("Go to Home") && src("../pages/Practice.jsx").includes("Go to Home") && src("../pages/Practice.jsx").includes("Home already shows"));
});
