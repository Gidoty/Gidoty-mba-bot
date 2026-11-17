import { useEffect, useId, useRef, useState } from "react";

// An "i" button that opens a plain-language help dialog. Works with the keyboard and screen readers: the button names what it explains, the dialog is labelled by its title,
// focus moves into it and comes back to the button, Tab stays inside, Escape and the Close buttons and a tap outside all close it, and the page behind does not scroll.
// On a phone it is a bottom sheet with 16 px text that scrolls when the text is long.
const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export default function InfoHelp({ label, help }) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef(null);
  const panelRef = useRef(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return undefined;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const first = panelRef.current?.querySelector("button");
    first?.focus();
    function onKey(e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      } else if (e.key === "Tab" && panelRef.current) {
        const items = Array.from(panelRef.current.querySelectorAll(FOCUSABLE));
        if (items.length === 0) return;
        const firstItem = items[0];
        const lastItem = items[items.length - 1];
        if (e.shiftKey && document.activeElement === firstItem) {
          e.preventDefault();
          lastItem.focus();
        } else if (!e.shiftKey && document.activeElement === lastItem) {
          e.preventDefault();
          firstItem.focus();
        }
      }
    }
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = previous;
      triggerRef.current?.focus();
    };
  }, [open]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`More information: ${label}`}
        className="inline-flex h-11 w-11 items-center justify-center rounded-full text-brand-text hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 11v5M12 8h.01" />
        </svg>
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center sm:p-4" onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}>
          <div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            className="max-h-[90vh] w-full overflow-y-auto rounded-t-xl bg-surface p-5 text-base leading-relaxed text-fg shadow-xl sm:max-w-lg sm:rounded-xl"
          >
            <div className="mb-3 flex items-start justify-between gap-3">
              <h2 id={titleId} className="text-lg font-semibold">
                {help.title}
              </h2>
              <button type="button" onClick={() => setOpen(false)} className="min-h-[44px] shrink-0 rounded-ctl border border-line-strong px-4 text-sm font-semibold hover:bg-surface-2">
                Close
              </button>
            </div>
            <div className="grid gap-3">
              {help.blocks.map((b, i) => (
                <div key={i}>
                  <p>
                    {b.title && <strong>{b.title}. </strong>}
                    {b.text}
                  </p>
                  {b.bullets && (
                    <ul className="mt-1 list-disc space-y-1 pl-5">
                      {b.bullets.map((t, j) => (
                        <li key={j}>{t}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
            <div className="mt-4 text-right">
              <button type="button" onClick={() => setOpen(false)} className="min-h-[44px] rounded-ctl bg-brand px-5 text-sm font-semibold text-brand-ink hover:brightness-105">
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
