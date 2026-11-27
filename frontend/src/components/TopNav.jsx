import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { DESKTOP_TABS, HOME_ROUTE, desktopActive, desktopSplit, visibleTabCount } from "../lib/navigation.js";
import { navigate } from "../useHashRoute.js";
import BrandLogo from "./BrandLogo.jsx";

const TAB_BASE = "relative flex h-14 items-center whitespace-nowrap px-3 text-sm font-medium";

// The top navigation bar from 768 px: the logo at the left, the tabs in ONE row, and a More menu at the right. The tabs never wrap and never scroll sideways: the ones that do not fit move into the More menu, rightmost
// first. Every width is measured from a hidden copy of the tabs, so the split follows the real text and font.
export default function TopNav({ route, onLogout, showAdmin = false }) {
  const [visible, setVisible] = useState(DESKTOP_TABS.length);
  const listRef = useRef(null);
  const measureRef = useRef(null);

  useLayoutEffect(() => {
    const measure = () => {
      const list = listRef.current;
      const copy = measureRef.current;
      if (!list || !copy) return;
      const widths = [...copy.children].map((c) => c.getBoundingClientRect().width);
      setVisible(visibleTabCount(widths, list.clientWidth));
    };
    measure();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    if (observer && listRef.current) observer.observe(listRef.current);
    window.addEventListener("resize", measure);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  const { tabs, overflow, menu } = desktopSplit(visible, showAdmin);
  const active = desktopActive(route, visible, showAdmin);

  return (
    <header className="sticky top-0 z-40 hidden border-b border-line bg-surface shell:block">
      <div className="mx-auto flex h-14 max-w-[1280px] items-center gap-6 px-6">
        <button type="button" onClick={() => navigate(HOME_ROUTE)} className="shrink-0 text-left" aria-label="Metabridge Bot, Home">
          <BrandLogo dark={false} />
        </button>
        <nav aria-label="Main" className="flex h-14 min-w-0 flex-1 items-center">
          <ul ref={listRef} className="flex h-14 min-w-0 flex-1 flex-nowrap overflow-hidden">
            {tabs.map((t) => (
              <li key={t.route} className="shrink-0">
                <a href={`#/${t.route}`} aria-current={active === t.route ? "page" : undefined} className={`${TAB_BASE} ${active === t.route ? "text-brand-text" : "text-muted hover:text-fg"}`}>
                  {t.label}
                  {active === t.route && <span aria-hidden="true" className="absolute inset-x-3 bottom-0 h-0.5 bg-accent-line" />}
                </a>
              </li>
            ))}
          </ul>
          <MoreMenu overflow={overflow} items={menu} route={route} active={active === "more"} onLogout={onLogout} />
        </nav>
      </div>
      <div ref={measureRef} aria-hidden="true" className="pointer-events-none invisible absolute left-0 top-0 flex whitespace-nowrap" style={{ height: 0, overflow: "hidden" }}>
        {DESKTOP_TABS.map((t) => (
          <span key={t.route} className={TAB_BASE}>
            {t.label}
          </span>
        ))}
      </div>
    </header>
  );
}

// The More menu: the tabs that did not fit, then Notifications, Testnet / Practice, Help & Guide, Account (and Admin for an admin), then Log out. Keyboard: Enter, Space or the down arrow opens it and focuses the first entry;
// the arrows, Home and End move between entries; Escape closes it and gives the focus back to the More button; Tab and a click outside close it.
function MoreMenu({ overflow, items, route, active, onLogout }) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef(null);
  const menuRef = useRef(null);
  const entries = () => [...(menuRef.current?.querySelectorAll('[role="menuitem"]') || [])];

  const close = useCallback((returnFocus) => {
    setOpen(false);
    if (returnFocus) buttonRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    entries()[0]?.focus();
    const outside = (e) => {
      if (!menuRef.current?.contains(e.target) && !buttonRef.current?.contains(e.target)) close(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open, close]);

  function onButtonKey(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
    }
  }

  function onMenuKey(e) {
    const list = entries();
    const i = list.indexOf(document.activeElement);
    if (e.key === "Escape") {
      e.preventDefault();
      close(true);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      list[(i + 1) % list.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      list[(i - 1 + list.length) % list.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      list[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      list[list.length - 1]?.focus();
    } else if (e.key === "Tab") {
      close(false);
    }
  }

  const item = "flex h-11 w-full items-center px-4 text-left text-sm font-medium focus:outline-none focus-visible:bg-surface-2";
  const entry = (t) => (
    <a key={t.route} role="menuitem" tabIndex={-1} href={`#/${t.route}`} aria-current={route === t.route ? "page" : undefined} onClick={() => close(false)} className={`${item} ${route === t.route ? "text-brand-text" : "text-fg"} hover:bg-surface-2`}>
      {t.label}
    </a>
  );

  return (
    <div className="relative shrink-0">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls="top-more-menu"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onButtonKey}
        className={`${TAB_BASE} gap-1 ${active ? "text-brand-text" : "text-muted hover:text-fg"}`}
      >
        More
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="h-4 w-4">
          <path d="M6 9l6 6 6-6" />
        </svg>
        {active && <span aria-hidden="true" className="absolute inset-x-3 bottom-0 h-0.5 bg-accent-line" />}
      </button>
      {open && (
        <div ref={menuRef} id="top-more-menu" role="menu" aria-label="More" onKeyDown={onMenuKey} className="absolute right-0 top-full z-50 mt-1 w-60 overflow-hidden rounded-card border border-line bg-surface py-1 shadow-lg">
          {overflow.map(entry)}
          {overflow.length > 0 && <div role="separator" className="my-1 border-t border-line" />}
          {items.map(entry)}
          <div role="separator" className="my-1 border-t border-line" />
          <button type="button" role="menuitem" tabIndex={-1} onClick={() => { close(false); onLogout(); }} className={`${item} text-fg hover:bg-surface-2`}>
            Log out
          </button>
        </div>
      )}
    </div>
  );
}
