import { useEffect } from "react";

import { BOTTOM_TABS, HOME_ROUTE, activeTab } from "../lib/navigation.js";
import { navigate } from "../useHashRoute.js";
import BrandLogo from "./BrandLogo.jsx";
import NavIcon from "./navIcons.jsx";
import TopNav from "./TopNav.jsx";
import { Pill } from "./ui.jsx";

// Pages already moved to the wide layout (charts and tables). Each page PR adds its route here.
const WIDE_ROUTES = new Set(["home", "positions", "trade-history", "performance", "practice"]);

// Wraps every signed-in page. From 768 px: a sticky top navigation bar (tabs in one row and a More menu) over a centred column. Below that: a small top bar, a fixed bottom bar (four tabs and More, which is a page) and the
// same column. Pages render only their own content.
export default function AppShell({ route, onLogout, botCode, showAdmin = false, children }) {
  const wide = WIDE_ROUTES.has(route) || route === "admin";
  const tab = activeTab(route);

  // Lets the floating help button sit above the bottom bar on a phone (see index.css).
  useEffect(() => {
    document.body.setAttribute("data-shell", "1");
    return () => document.body.removeAttribute("data-shell");
  }, []);

  return (
    <div className="min-h-screen bg-app text-fg">
      <TopNav route={route} onLogout={onLogout} showAdmin={showAdmin} />

      <header className="sticky top-0 z-30 flex items-center justify-between bg-rail px-4 py-2.5 shell:hidden">
        <button type="button" onClick={() => navigate(HOME_ROUTE)} className="text-left">
          <BrandLogo subtitle={botCode || null} />
        </button>
        <span id="advice-slot-mobile" className="contents" />
      </header>
      <div className={`mx-auto hidden items-center justify-end gap-3 px-6 pt-5 shell:flex ${wide ? "max-w-[1120px]" : "max-w-3xl"}`}>
        <span id="advice-slot-desktop" className="contents" />
        {botCode && <Pill>{botCode}</Pill>}
      </div>
      <main className={`mx-auto px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom))] pt-5 shell:px-6 shell:pb-12 ${wide ? "max-w-[1120px]" : "max-w-3xl"}`}>{children}</main>

      <nav aria-label="Main" className="fixed inset-x-0 bottom-0 z-30 flex border-t border-line bg-surface pb-[env(safe-area-inset-bottom)] shell:hidden">
        {BOTTOM_TABS.map((t) => (
          <TabLink key={t.route} item={t} active={tab === t.route} />
        ))}
      </nav>
    </div>
  );
}

// One item of the bottom bar: a 22 px icon with its label under it (11 px, never cut off: the item is as wide as its label), at least 56 px tall and 44 px wide. Active: the brand colour and a short bar at the top edge.
function TabLink({ item, active }) {
  return (
    <button
      type="button"
      onClick={() => navigate(item.route)}
      aria-current={active ? "page" : undefined}
      className={`relative flex min-h-[56px] min-w-[44px] flex-1 basis-auto flex-col items-center justify-center gap-1 whitespace-nowrap px-1 text-[11px] font-medium leading-none ${active ? "text-brand-text" : "text-muted"}`}
    >
      {active && <span aria-hidden="true" className="absolute left-1/2 top-0 h-[3px] w-7 -translate-x-1/2 rounded-b bg-accent-line" />}
      <NavIcon route={item.route} />
      <span>{item.label}</span>
    </button>
  );
}
