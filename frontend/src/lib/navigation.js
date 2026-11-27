// The app's navigation, as data: the phone's bottom bar (Home, Positions, History, More), the pages inside More (grouped), the desktop top bar (seven tabs and a More menu), and the legal links that live on the Account page.
// Pure: no React. Run with: node --test frontend/src/lib/navigation.test.js (also run by tests/test_frontend_lib.py)
import { LEGAL_NAV_ITEMS, NAV_ITEMS } from "../constants.js";

export const HOME_ROUTE = "home";
export const MORE_ROUTE = "more";
export const ADMIN_ROUTE = "admin";

// Old links and bookmarks: #/dashboard still opens Home.
const LEGACY_ROUTES = { dashboard: HOME_ROUTE };

export function canonicalRoute(route) {
  return LEGACY_ROUTES[route] || route;
}

const label = (route) => NAV_ITEMS.find((i) => i.route === route)?.label || route;

// The phone's bottom bar: exactly four items, in this order: Home, Positions, History, More. "More" is a page, not a menu. Subscriptions and Performance are inside More.
export const BOTTOM_TABS = ["home", "positions", "trade-history"].map((route) => ({ route, label: label(route) })).concat([{ route: MORE_ROUTE, label: "More" }]);

// The pages inside More on the phone, in groups. Nothing that exists as a page is dropped: the admin page joins when the customer is an admin.
export const MORE_GROUPS = [
  { title: "Billing", items: [{ route: "subscriptions", label: label("subscriptions") }] },
  { title: "Trading", items: ["performance", "bot-settings", "exchange-connection", "practice"].map((route) => ({ route, label: label(route) })) },
  { title: "Alerts and help", items: ["notifications", "help"].map((route) => ({ route, label: label(route) })) },
  { title: "Account", items: [{ route: "account", label: label("account") }] },
];

export function moreGroups(showAdmin = false) {
  return showAdmin ? [...MORE_GROUPS, { title: "Admin", items: [{ route: ADMIN_ROUTE, label: "Admin" }] }] : MORE_GROUPS;
}

// The laptop and desktop top bar (from 768 px): seven tabs in one row, and a More menu at the right. When the tabs do not fit, the rightmost move into the menu (see visibleTabCount).
export const DESKTOP_TABS = ["home", "positions", "trade-history", "performance", "subscriptions", "bot-settings", "exchange-connection"].map((route) => ({ route, label: label(route) }));
const DESKTOP_MENU = ["notifications", "practice", "help", "account"].map((route) => ({ route, label: label(route) }));

// What the More menu holds besides the tabs that did not fit: Notifications, Testnet / Practice, Help & Guide, Account, and Admin for an admin. Log out is the last entry of the menu itself.
export function desktopMenuItems(showAdmin = false) {
  return showAdmin ? [...DESKTOP_MENU, { route: ADMIN_ROUTE, label: "Admin" }] : DESKTOP_MENU;
}

// How many tabs fit in one row: the largest n whose widths (and the gap between them) add up to at most `available`. Never wraps and never needs a scrollbar: whatever does not fit goes into the More menu.
export function visibleTabCount(widths, available, gap = 0) {
  let used = 0;
  let n = 0;
  for (const w of widths) {
    const next = used + (n > 0 ? gap : 0) + w;
    if (!(next <= available)) break;
    used = next;
    n += 1;
  }
  return n;
}

// The desktop split for a given number of visible tabs: the tabs shown, and the menu's entries (the tabs that did not fit first, then the standing items).
export function desktopSplit(visibleCount, showAdmin = false) {
  const n = Math.max(0, Math.min(DESKTOP_TABS.length, visibleCount));
  return { tabs: DESKTOP_TABS.slice(0, n), overflow: DESKTOP_TABS.slice(n), menu: desktopMenuItems(showAdmin) };
}

// Which desktop item is active: the tab itself when it is shown, otherwise "more" (the page is inside the menu: an overflowed tab, Notifications, Testnet / Practice, Help & Guide, Account or Admin).
export function desktopActive(route, visibleCount, showAdmin = true) {
  const r = canonicalRoute(route);
  const { tabs, overflow, menu } = desktopSplit(visibleCount, showAdmin);
  if (tabs.some((t) => t.route === r)) return r;
  if (overflow.some((t) => t.route === r) || menu.some((m) => m.route === r) || r === MORE_ROUTE) return MORE_ROUTE;
  return HOME_ROUTE;
}

export function isMoreRoute(route, showAdmin = true) {
  return route === MORE_ROUTE || moreGroups(showAdmin).some((g) => g.items.some((i) => i.route === route));
}

// Which bottom-bar item is active for the page on screen: the page's own tab, "more" for More and every page inside it (Subscriptions and Performance included), and Home for anything unknown.
export function activeTab(route) {
  const r = canonicalRoute(route);
  if (BOTTOM_TABS.some((t) => t.route === r && t.route !== MORE_ROUTE)) return r;
  if (isMoreRoute(r)) return MORE_ROUTE;
  return HOME_ROUTE;
}

// Terms, Privacy and the Risk Disclosure are shown at the bottom of the Account page (and at sign-up and sign-in, where consent is given), not in a footer or a menu.
export const LEGAL_LINKS = LEGAL_NAV_ITEMS;
