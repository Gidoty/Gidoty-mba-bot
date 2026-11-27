// The app's navigation, as data: the six items of the phone's bottom bar, the pages inside More (grouped), the desktop sidebar (the same names in the same order), and the legal links that live on the Account page.
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

// Exactly six, in this order. "More" is a page, not a menu.
export const BOTTOM_TABS = ["home", "positions", "trade-history", "subscriptions", "performance"].map((route) => ({ route, label: label(route) })).concat([{ route: MORE_ROUTE, label: "More" }]);

// The pages inside More, in groups. Nothing that exists as a tab is dropped: the admin page joins when the customer is an admin.
export const MORE_GROUPS = [
  { title: "Trading", items: ["bot-settings", "exchange-connection", "practice"].map((route) => ({ route, label: label(route) })) },
  { title: "Alerts and help", items: ["notifications", "help"].map((route) => ({ route, label: label(route) })) },
  { title: "Account", items: [{ route: "account", label: label("account") }] },
];

export function moreGroups(showAdmin = false) {
  return showAdmin ? [...MORE_GROUPS, { title: "Admin", items: [{ route: ADMIN_ROUTE, label: "Admin" }] }] : MORE_GROUPS;
}

// The desktop sidebar: the five main items, then everything inside More, in More's order.
export function sidebarItems(showAdmin = false) {
  const main = BOTTOM_TABS.filter((t) => t.route !== MORE_ROUTE);
  return [...main, ...moreGroups(showAdmin).flatMap((g) => g.items)];
}

export function isMoreRoute(route, showAdmin = true) {
  return route === MORE_ROUTE || moreGroups(showAdmin).some((g) => g.items.some((i) => i.route === route));
}

// Which bottom-bar item is active for the page on screen: the page's own tab, "more" for More and every page inside it, and Home for anything unknown.
export function activeTab(route) {
  const r = canonicalRoute(route);
  if (BOTTOM_TABS.some((t) => t.route === r && t.route !== MORE_ROUTE)) return r;
  if (isMoreRoute(r)) return MORE_ROUTE;
  return HOME_ROUTE;
}

// Terms, Privacy and the Risk Disclosure are shown at the bottom of the Account page (and at sign-up and sign-in, where consent is given), not in a footer or the sidebar.
export const LEGAL_LINKS = LEGAL_NAV_ITEMS;
