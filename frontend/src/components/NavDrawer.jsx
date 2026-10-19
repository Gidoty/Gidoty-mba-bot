import { useState } from "react";

import { LEGAL_NAV_ITEMS, NAV_ITEMS } from "../constants.js";
import { navigate } from "../useHashRoute.js";
import BrandLogo from "./BrandLogo.jsx";

// A single hamburger/drawer pattern on every breakpoint (approved for
// this version rather than a separate desktop sidebar) - one
// implementation to get right instead of two to keep in sync.
export default function NavDrawer({ route, onLogout, botCode }) {
  const [open, setOpen] = useState(false);

  function go(nextRoute) {
    navigate(nextRoute);
    setOpen(false);
  }

  return (
    <>
      <header className="sticky top-0 z-30 flex items-center justify-between bg-ink px-4 py-3 shadow-sm">
        <button type="button" onClick={() => go("dashboard")} className="text-left">
          <BrandLogo subtitle={botCode || null} />
        </button>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open menu"
          className="flex h-10 w-10 items-center justify-center rounded-md text-white hover:bg-white/10"
        >
          <span className="sr-only">Menu</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-6 w-6">
            <path strokeLinecap="round" d="M4 7h16M4 12h16M4 17h16" />
          </svg>
        </button>
      </header>

      {open && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/50" onClick={() => setOpen(false)}>
          <nav
            className="flex h-full w-full max-w-xs flex-col bg-white shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-slate-200 bg-ink px-4 py-3">
              <BrandLogo />
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close menu"
                className="flex h-9 w-9 items-center justify-center rounded-md text-white hover:bg-white/10"
              >
                ✕
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-2 py-3">
              <ul className="space-y-1">
                {NAV_ITEMS.map((item) => (
                  <li key={item.route}>
                    <NavLink item={item} active={route === item.route} onClick={() => go(item.route)} />
                  </li>
                ))}
              </ul>

              <p className="mt-5 mb-1 px-3 text-xs font-semibold uppercase tracking-wide text-slate-400">Legal</p>
              <ul className="space-y-1">
                {LEGAL_NAV_ITEMS.map((item) => (
                  <li key={item.route}>
                    <NavLink item={item} active={route === item.route} onClick={() => go(item.route)} />
                  </li>
                ))}
              </ul>
            </div>

            <div className="border-t border-slate-200 p-3">
              <button
                type="button"
                onClick={onLogout}
                className="w-full rounded-md px-3 py-2.5 text-left text-sm font-medium text-slate-600 hover:bg-slate-50"
              >
                Log out
              </button>
            </div>
          </nav>
        </div>
      )}
    </>
  );
}

function NavLink({ item, active, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`block w-full rounded-md px-3 py-2.5 text-left text-sm font-medium ${
        active ? "bg-accent/10 text-accent-dark" : "text-slate-700 hover:bg-slate-50"
      }`}
    >
      {item.label}
    </button>
  );
}
