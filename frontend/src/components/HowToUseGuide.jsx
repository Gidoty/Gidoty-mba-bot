import { useEffect, useRef, useState } from "react";

import { METABRIDGE_SERVER_IP } from "../constants.js";
import { howToSections } from "../howTo.js";

export default function HowToUseGuide() {
  const [open, setOpen] = useState(false);
  const closeRef = useRef(null);
  const sections = howToSections(METABRIDGE_SERVER_IP);

  useEffect(() => {
    if (!open) return undefined;
    closeRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  let n = 0;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="how-to-fab fixed bottom-4 right-4 z-40 flex items-center gap-1.5 rounded-full bg-ink px-4 py-2 text-xs font-semibold text-white shadow-lg hover:bg-ink/90"
      >
        <span className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-white/70 text-[10px] leading-none">
          ?
        </span>
        How to use this bot
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-0 sm:items-center sm:p-4" onClick={() => setOpen(false)}>
          <div
            role="dialog"
            aria-modal="true"
            aria-label="How to use Metabridge Trading Bot"
            className="flex max-h-[90vh] w-full max-w-2xl flex-col rounded-t-card border border-line bg-surface shadow-xl sm:rounded-card"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
              <div>
                <h2 className="text-lg font-bold tracking-tight text-fg">How to use Metabridge Trading Bot</h2>
                <p className="mt-0.5 text-xs text-muted">Step by step, in the order you will meet the screens.</p>
              </div>
              <button ref={closeRef} type="button" onClick={() => setOpen(false)} aria-label="Close" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-ctl text-fg hover:bg-surface-2">
                ✕
              </button>
            </div>

            <div className="space-y-6 overflow-y-auto px-5 py-5">
              {sections.map((section) => (
                <section key={section.title}>
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">{section.title}</h3>
                  <ol className="mt-3 space-y-4">
                    {section.steps.map(([title, body]) => {
                      n += 1;
                      return (
                        <li key={title} className="flex gap-3">
                          <span aria-hidden="true" className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand text-xs font-semibold text-brand-ink">
                            {n}
                          </span>
                          <div className="min-w-0">
                            <p className="text-sm font-semibold text-fg">{title}</p>
                            <p className="mt-0.5 text-sm text-muted">{body}</p>
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                </section>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
