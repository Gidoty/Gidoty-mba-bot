import { useEffect, useRef, useState } from "react";

import { LEGAL_NAV_ITEMS, NAV_ITEMS } from "../constants.js";
import { navigate } from "../useHashRoute.js";
import BrandLogo from "./BrandLogo.jsx";
import NavIcon from "./navIcons.jsx";
import { Pill } from "./ui.jsx";

// Pages already moved to the wide layout (charts and tables). Each page PR adds its route here.
const WIDE_ROUTES = new Set(["dashboard", "positions", "trade-history", "performance", "practice"]);

// The four routes that get a tab on a phone; everything else sits under "More".
const TAB_ROUTES = ["dashboard", "positions", "trade-history", "performance"];
const TAB_LABELS = { "trade-history": "History" };

const label = (route) => NAV_ITEMS.find((i) => i.route === route)?.label || route;

// Wraps every signed-in page: sidebar from 900 px, a top bar and bottom tab bar
// below that, a theme switch, and one content width. Pages render only their own content.
export default function AppShell({ route, onLogout, botCode, children }) {
  const [moreOpen, setMoreOpen] = useState(false);
  const wide = WIDE_ROUTES.has(route);

  // Lets the floating help button sit above the tab bar on a phone (see index.css).
  useEffect(() => {
    document.body.setAttribute("data-shell", "1");
    return () => document.body.removeAttribute("data-shell");
  }, []);

  return (
    <div className="min-h-screen bg-app text-fg shell:grid shell:grid-cols-[224px_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-screen flex-col gap-1 bg-rail px-3 py-5 shell:flex">
        <button type="button" onClick={() => navigate("dashboard")} className="mb-5 px-2 text-left">
          <BrandLogo />
        </button>
        <nav aria-label="Main" className="flex-1 space-y-0.5 overflow-y-auto">
          {NAV_ITEMS.map((item) => (
            <SideLink key={item.route} item={item} active={route === item.route} />
          ))}
        </nav>
        <div className="space-y-2 px-1 pt-3">
          <p className="px-2 text-[11px] font-semibold uppercase tracking-wider text-rail-text/70">Legal</p>
          {LEGAL_NAV_ITEMS.map((item) => (
            <button key={item.route} type="button" onClick={() => navigate(item.route)} className="block px-2 text-left text-xs text-rail-text hover:text-white hover:underline">
              {item.label}
            </button>
          ))}
          <button type="button" onClick={onLogout} className="mt-2 w-full rounded-ctl border border-white/20 py-2 text-sm font-medium text-rail-text hover:bg-rail-active">
            Log out
          </button>
        </div>
      </aside>

      <div className="min-w-0">
        <header className="sticky top-0 z-30 flex items-center justify-between bg-rail px-4 py-2.5 shell:hidden">
          <button type="button" onClick={() => navigate("dashboard")} className="text-left">
            <BrandLogo subtitle={botCode || null} />
          </button>
        </header>
        <div className="hidden items-center justify-end gap-3 px-6 pt-5 shell:flex">
          {botCode && <Pill>{botCode}</Pill>}
        </div>
        <main className={`mx-auto px-4 pb-28 pt-5 shell:px-6 shell:pb-12 ${wide ? "max-w-[1120px]" : "max-w-3xl"}`}>{children}</main>
      </div>

      <nav aria-label="Main" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-white/10 bg-rail shell:hidden">
        {TAB_ROUTES.map((r) => (
          <TabLink key={r} route={r} text={TAB_LABELS[r] || label(r)} active={route === r} onClick={() => navigate(r)} />
        ))}
        <TabLink route="more" text="More" active={!TAB_ROUTES.includes(route)} onClick={() => setMoreOpen(true)} />
      </nav>

      {moreOpen && <MoreSheet route={route} onClose={() => setMoreOpen(false)} onLogout={onLogout} />}
    </div>
  );
}

function SideLink({ item, active }) {
  return (
    <button
      type="button"
      onClick={() => navigate(item.route)}
      aria-current={active ? "page" : undefined}
      className={`flex w-full items-center gap-3 rounded-ctl px-3 py-2.5 text-left text-sm font-medium ${
        active ? "bg-rail-active text-white shadow-[inset_3px_0_0_var(--accent)]" : "text-rail-text hover:bg-rail-active"
      }`}
    >
      <NavIcon route={item.route} />
      {item.label}
    </button>
  );
}

function TabLink({ route, text, active, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={`flex min-h-[3.4rem] flex-col items-center justify-center gap-0.5 text-[11px] font-medium ${active ? "text-accent-light" : "text-rail-text"}`}
    >
      <NavIcon route={route} className="h-[22px] w-[22px]" />
      {text}
    </button>
  );
}

// Everything that has no tab: the other pages, the legal documents, the theme and log out.
function MoreSheet({ route, onClose, onLogout }) {
  const closeRef = useRef(null);
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const go = (r) => {
    navigate(r);
    onClose();
  };
  const rest = NAV_ITEMS.filter((i) => !TAB_ROUTES.includes(i.route));

  return (
    <div className="fixed inset-0 z-40 flex items-end bg-black/50" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label="More" className="max-h-[85vh] w-full overflow-y-auto rounded-t-card border-t border-line bg-surface p-4" onClick={(e) => e.stopPropagation()}>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-muted">More</h2>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close" className="flex h-9 w-9 items-center justify-center rounded-ctl text-fg hover:bg-surface-2">
            ✕
          </button>
        </div>
        <ul className="space-y-0.5">
          {rest.map((item) => (
            <li key={item.route}>
              <button type="button" onClick={() => go(item.route)} aria-current={route === item.route ? "page" : undefined} className={`flex w-full items-center gap-3 rounded-ctl px-3 py-3 text-left text-sm font-medium ${route === item.route ? "bg-brand-soft text-brand-text" : "text-fg hover:bg-surface-2"}`}>
                <NavIcon route={item.route} />
                {item.label}
              </button>
            </li>
          ))}
        </ul>
        <p className="mb-1 mt-4 px-3 text-xs font-semibold uppercase tracking-wider text-muted">Legal</p>
        <ul className="space-y-0.5">
          {LEGAL_NAV_ITEMS.map((item) => (
            <li key={item.route}>
              <button type="button" onClick={() => go(item.route)} className="w-full rounded-ctl px-3 py-2.5 text-left text-sm text-fg hover:bg-surface-2">
                {item.label}
              </button>
            </li>
          ))}
        </ul>
        <div className="mt-4 flex items-center justify-end gap-3 border-t border-line pt-4">
          <button type="button" onClick={onLogout} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium text-fg hover:bg-surface-2">
            Log out
          </button>
        </div>
      </div>
    </div>
  );
}
