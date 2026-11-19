import { useRef } from "react";

import { nextTabIndex } from "../lib/lossLimit.js";

// An accessible tab list (WAI-ARIA tabs): one tab is in the tab order, the arrow keys move between tabs and wrap, Home and End jump, and each tab names its panel.
// `marks` maps a tab id to "error" or "changed" so a tab that needs attention says so in words, not only in colour. The parent renders the panels (see tabPanelProps).
export function tabId(id) {
  return `settings-tab-${id}`;
}
export function panelId(id) {
  return `settings-panel-${id}`;
}
export function tabPanelProps(id, active) {
  return { id: panelId(id), role: "tabpanel", "aria-labelledby": tabId(id), hidden: id !== active, tabIndex: 0 };
}

const MARK_TEXT = { error: "needs attention", changed: "unsaved changes" };

export default function SettingsTabs({ tabs, active, onChange, marks = {}, label = "Settings sections", right = null }) {
  const refs = useRef({});
  function onKeyDown(e, index) {
    const next = nextTabIndex(e.key, index, tabs.length);
    if (next === null) return;
    e.preventDefault();
    onChange(tabs[next].id);
    refs.current[tabs[next].id]?.focus();
  }
  return (
    <div role="tablist" aria-label={label} className="flex gap-1 border-b border-line">
      {tabs.map((t, i) => {
        const selected = t.id === active;
        const mark = marks[t.id];
        return (
          <button
            key={t.id}
            ref={(el) => (refs.current[t.id] = el)}
            id={tabId(t.id)}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={panelId(t.id)}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => onKeyDown(e, i)}
            className={`-mb-px min-h-[44px] border-b-2 px-4 text-sm font-semibold ${selected ? "border-brand text-brand-text" : "border-transparent text-muted hover:text-fg"}`}
          >
            {t.label}
            {mark && (
              <>
                <span aria-hidden="true" className={`ml-1.5 inline-block h-2 w-2 rounded-full align-middle ${mark === "error" ? "bg-neg" : "bg-warn"}`} />
                <span className="sr-only"> ({MARK_TEXT[mark]})</span>
              </>
            )}
          </button>
        );
      })}
      {right && <div className="ml-auto flex items-center self-center pl-2">{right}</div>}
    </div>
  );
}
