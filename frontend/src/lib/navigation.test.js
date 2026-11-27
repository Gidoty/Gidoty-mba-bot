import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import test from "node:test";

import { NAV_ITEMS } from "../constants.js";
import { ICON_PATHS, ICON_STROKE } from "./navIconPaths.js";
import { BOTTOM_TABS, DESKTOP_TABS, HOME_ROUTE, LEGAL_LINKS, MORE_GROUPS, activeTab, canonicalRoute, desktopActive, desktopMenuItems, desktopSplit, isMoreRoute, moreGroups, visibleTabCount } from "./navigation.js";
import { SUBSCRIPTIONS_ROUTE } from "./subscriptions.js";

const src = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const codeOf = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "").replace(/ \/\/ .*$/gm, "");

test("the phone's bottom bar has exactly four items: Home, Positions, History, More", () => {
  assert.deepEqual(BOTTOM_TABS.map((t) => t.label), ["Home", "Positions", "History", "More"]);
  assert.deepEqual(BOTTOM_TABS.map((t) => t.route), ["home", "positions", "trade-history", "more"]);
  assert.equal(BOTTOM_TABS.length, 4);
  assert.ok(!BOTTOM_TABS.some((t) => t.route === "performance" || t.route === "subscriptions"));
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
  assert.deepEqual(MORE_GROUPS.map((g) => g.title), ["Billing", "Trading", "Alerts and help", "Account"]);
  assert.deepEqual(MORE_GROUPS.flatMap((g) => g.items.map((i) => i.label)), ["Subscriptions", "Performance", "Bot Settings", "Exchange Connection", "Testnet / Practice", "Notifications", "Help & Guide", "Account"]);
  assert.deepEqual(MORE_GROUPS[0].items.map((i) => i.route), ["subscriptions"], "Subscriptions is alone in the first group, Billing");
  assert.equal(MORE_GROUPS[1].items[0].route, "performance", "Performance is the first row of the Trading group");
  const tabRoutes = new Set(BOTTOM_TABS.map((t) => t.route));
  const inMore = new Set(MORE_GROUPS.flatMap((g) => g.items.map((i) => i.route)));
  for (const item of NAV_ITEMS) assert.ok(tabRoutes.has(item.route) || inMore.has(item.route), `${item.route} must be a tab or inside More`);
  assert.deepEqual(moreGroups(true).at(-1), { title: "Admin", items: [{ route: "admin", label: "Admin" }] }); // the admin page joins for an admin
  assert.equal(moreGroups(false).length, 4);
});

test("the desktop top bar has seven tabs and a More menu, with the same names as the phone", () => {
  assert.deepEqual(DESKTOP_TABS.map((t) => t.label), ["Home", "Positions", "History", "Performance", "Subscriptions", "Bot Settings", "Exchange Connection"]);
  assert.deepEqual(desktopMenuItems().map((i) => i.label), ["Notifications", "Testnet / Practice", "Help & Guide", "Account"]);
  assert.deepEqual(desktopMenuItems(true).at(-1), { route: "admin", label: "Admin" });
  assert.equal(desktopMenuItems(false).some((i) => i.route === "admin"), false);
  // every page reachable today is reachable on the desktop and on the phone
  const phone = new Set([...BOTTOM_TABS.map((t) => t.route), ...MORE_GROUPS.flatMap((g) => g.items.map((i) => i.route))]);
  const desk = new Set([...DESKTOP_TABS.map((t) => t.route), ...desktopMenuItems().map((i) => i.route)]);
  for (const item of NAV_ITEMS) {
    assert.ok(desk.has(item.route), `${item.route} on the desktop`);
    assert.ok(item.route === "home" ? phone.has("home") : phone.has(item.route), `${item.route} on the phone`);
  }
  assert.deepEqual(NAV_ITEMS.map((i) => i.label), [...DESKTOP_TABS, ...desktopMenuItems()].map((i) => i.label));
  const shell = codeOf(src("../components/AppShell.jsx")) + codeOf(src("../components/TopNav.jsx"));
  assert.ok(!/terms|privacy|risk-disclosure|LEGAL/i.test(shell), "no legal links in the bars or the menu");
});

test("tabs go into the More menu from the right when they do not fit, and never wrap", () => {
  const widths = [60, 80, 70, 100, 110, 100, 160];
  assert.equal(visibleTabCount(widths, 1000), 7);
  assert.equal(visibleTabCount(widths, 680), 7);
  assert.equal(visibleTabCount(widths, 679), 6); // the rightmost goes first
  assert.equal(visibleTabCount(widths, 520), 6);
  assert.equal(visibleTabCount(widths, 519), 5);
  assert.equal(visibleTabCount(widths, 60), 1);
  assert.equal(visibleTabCount(widths, 59), 0);
  assert.equal(visibleTabCount([], 100), 0);
  assert.equal(visibleTabCount([50, 50], 102, 2), 2); // the gap counts
  assert.equal(visibleTabCount([50, 50], 101, 2), 1);
  for (let a = 0; a < 800; a += 7) {
    const n = visibleTabCount(widths, a);
    assert.ok(widths.slice(0, n).reduce((x, y) => x + y, 0) <= a, `${n} tabs fit in ${a}`);
    if (n < widths.length) assert.ok(widths.slice(0, n + 1).reduce((x, y) => x + y, 0) > a, `one more does not fit in ${a}`);
  }
});

test("the split gives the overflowed tabs first in the menu, in their own order, then the standing items", () => {
  const all = desktopSplit(7);
  assert.equal(all.tabs.length, 7);
  assert.deepEqual(all.overflow, []);
  const some = desktopSplit(5);
  assert.deepEqual(some.tabs.map((t) => t.label), ["Home", "Positions", "History", "Performance", "Subscriptions"]);
  assert.deepEqual(some.overflow.map((t) => t.label), ["Bot Settings", "Exchange Connection"]);
  assert.deepEqual(some.menu.map((t) => t.label), ["Notifications", "Testnet / Practice", "Help & Guide", "Account"]);
  assert.equal(desktopSplit(0).tabs.length, 0);
  assert.equal(desktopSplit(99).tabs.length, 7);
  assert.equal(desktopSplit(-3).tabs.length, 0);
});

test("on the desktop the active item is the shown tab, or More when the page is inside the menu", () => {
  assert.equal(desktopActive("home", 7), "home");
  assert.equal(desktopActive("dashboard", 7), "home");
  assert.equal(desktopActive("exchange-connection", 7), "exchange-connection");
  assert.equal(desktopActive("exchange-connection", 5), "more"); // it moved into the menu
  assert.equal(desktopActive("bot-settings", 6), "bot-settings");
  for (const r of ["notifications", "practice", "help", "account", "admin", "more"]) assert.equal(desktopActive(r, 7), "more", r);
  assert.equal(desktopActive("nonsense", 7), "home");
});

test("no sidebar and no hamburger are left; the top bar is a real navigation with keyboard use", () => {
  const shell = src("../components/AppShell.jsx");
  const top = src("../components/TopNav.jsx");
  assert.ok(!/<aside|sidebar|SideLink|☰|hamburger/i.test(codeOf(shell) + codeOf(top)));
  assert.ok(!/aria-label="(Menu|Open menu|Navigation menu)"/i.test(shell + top));
  assert.ok(top.includes('<nav aria-label="Main"') && top.includes('aria-current={active === t.route ? "page" : undefined}'));
  assert.ok(top.includes('aria-haspopup="menu"') && top.includes("aria-expanded={open}") && top.includes('role="menu"') && top.includes('role="menuitem"'));
  for (const key of ['"Escape"', '"ArrowDown"', '"ArrowUp"', '"Home"', '"End"', '"Tab"']) assert.ok(top.includes(key), key);
  assert.ok(top.includes("buttonRef.current?.focus()"), "closing can give the focus back to the More button");
  assert.ok(/"Escape"\)\s*\{\s*e\.preventDefault\(\);\s*close\(true\)/.test(top), "Escape closes the menu and gives the focus back to the More button");
  assert.ok(top.includes('document.addEventListener("mousedown"'), "a click outside closes the menu");
  assert.ok(/>\s*Log out\s*</.test(top) && top.includes("onLogout"), "Log out is in the menu");
});

test("the top bar is sticky, from 768 px, one row, muted tabs and a brand underline; the phone layout stays below 768", () => {
  const top = src("../components/TopNav.jsx");
  const shell = src("../components/AppShell.jsx");
  assert.ok(top.includes("sticky top-0") && top.includes("hidden") && top.includes("shell:block"));
  assert.ok(top.includes("whitespace-nowrap") && top.includes("flex-nowrap") && top.includes("overflow-hidden"));
  assert.ok(!/flex-wrap|overflow-x-auto|overflow-x-scroll|overflow-auto|overflow-scroll/.test(top.slice(top.indexOf("export default function TopNav"))), "never wraps, never scrolls sideways");
  assert.ok(top.includes("text-muted") && top.includes("text-brand-text") && top.includes("h-0.5") && top.includes("bg-accent-line"));
  assert.equal(top.split("absolute inset-x-3 bottom-0 h-0.5 bg-accent-line").length - 1, 2, "the active tab and the active More button both have the 2 px underline");
  assert.equal(top.split('"text-brand-text" : "text-muted hover:text-fg"').length - 1, 2, "inactive tabs and the More button use the muted token");
  assert.ok(/<nav aria-label="Main" className="[^"]*shell:hidden/.test(shell), "the bottom bar is for phones only");
  assert.ok(/<header className="[^"]*shell:hidden/.test(shell), "so is the small top bar");
  assert.ok(shell.includes("BOTTOM_TABS.map") && shell.includes("TopNav"));
  const tailwind = readFileSync(new URL("../../tailwind.config.js", import.meta.url), "utf8");
  assert.ok(/shell:\s*"768px"/.test(tailwind));
  assert.ok(/max-width:\s*767px/.test(readFileSync(new URL("../index.css", import.meta.url), "utf8")));
  assert.ok(top.includes("visibleTabCount") && top.includes("ResizeObserver"));
});

test("the More item is active on More and on every page inside it, and a page with a tab keeps its own tab", () => {
  for (const g of MORE_GROUPS) for (const i of g.items) assert.equal(activeTab(i.route), "more", i.route);
  assert.equal(activeTab("more"), "more");
  assert.equal(activeTab("admin"), "more");
  assert.equal(activeTab("performance"), "more"); // Performance now lives inside More
  assert.equal(activeTab("positions"), "positions");
  assert.equal(activeTab("subscriptions"), "more"); // Subscriptions lives inside More too
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
  assert.equal(shell.split('aria-label="Main"').length - 1, 1, "the bottom bar is labelled Main");
  assert.equal(src("../components/TopNav.jsx").split('aria-label="Main"').length - 1, 1, "so is the top bar (only one of them is on screen at any width)");
  assert.equal(shell.split('aria-current={active ? "page" : undefined}').length - 1, 1, "the bottom bar marks the active item");
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

test("links that point to Subscriptions keep working and open a page inside More", () => {
  assert.equal(SUBSCRIPTIONS_ROUTE, "subscriptions");
  assert.ok(isMoreRoute(SUBSCRIPTIONS_ROUTE) && activeTab(SUBSCRIPTIONS_ROUTE) === "more");
  assert.ok(src("../App.jsx").includes("subscriptions: Subscriptions,"), "the page is still routed");
  for (const f of ["../components/SubscriptionStatusCard.jsx", "../pages/dashboard/TestnetDashboard.jsx"]) assert.ok(src(f).includes("navigate(SUBSCRIPTIONS_ROUTE)"), f);
  assert.ok(MORE_GROUPS[0].title === "Billing" && MORE_GROUPS[0].items[0].route === "subscriptions", "Billing is the first group in More");
});
