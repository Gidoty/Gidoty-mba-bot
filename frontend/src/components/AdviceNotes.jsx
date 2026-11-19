import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "../api/client.js";
import { buttonState, createReadStore, groupNotes, noteKey, requestKey } from "../lib/advice.js";
import { secondaryButton } from "./form.jsx";

const DEBOUNCE_MS = 500;

// The setup advisor (GET and POST /api/account/advice). `body` is what the customer is about to save (lib/advice.js buildRequest). The advisor is off unless the service turns it on: a 404 hides it,
// and so does any error, because it only informs and must never get in the way of the screen. One request after the customer stops typing; an older answer never replaces a newer one.
export function useAdvice(body) {
  const [state, setState] = useState({ notes: [], enabled: true });
  const key = requestKey(body);
  const latest = useRef(0);
  const bodyRef = useRef(body);
  bodyRef.current = body;
  useEffect(() => {
    if (state.enabled === false) return undefined;
    const id = ++latest.current;
    const timer = setTimeout(async () => {
      try {
        const r = await api.post("/account/advice", bodyRef.current);
        if (id === latest.current) setState({ notes: r.notes || [], enabled: true });
      } catch (err) {
        if (id !== latest.current) return;
        setState(err.status === 404 ? { notes: [], enabled: false } : (s) => ({ ...s, notes: [] }));
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

// ---------------------------------------------------------------------------------------------------- the quiet button and its pop-up
// Closed by default: one small button with a count. A click opens a dialog (centred panel on a desktop, bottom sheet on a phone) with the notes grouped by severity. Closing it, by any way, marks what was
// shown as read; the button then goes away until a note is new or its words changed. A "not possible" note keeps the button (red dot) while it exists, because it explains why Save will be refused.
// Opening is always a click: nothing opens by itself, nothing animates. Read state is a per-browser convenience (lib/advice.js createReadStore): note ids and hashes only.
const store = createReadStore();

function useReadSet() {
  const [read, setRead] = useState(store.snapshot());
  useEffect(() => {
    setRead(store.snapshot());
    return store.subscribe(() => setRead(store.snapshot()));
  }, []);
  return read;
}

const GROUP_DOT = { not_possible: "bg-neg", warning: "bg-warn", tip: "bg-info" };
const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function Dialog({ groups, action, onClose }) {
  const panel = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose; // the effect below runs once: a re-render of the screen must not pull focus back to the panel
  useEffect(() => {
    panel.current?.focus();
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden"; // the page behind does not scroll while the dialog is open
    function onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab" || !panel.current) return;
      const items = [...panel.current.querySelectorAll(FOCUSABLE)];
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, []);

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center sm:p-4" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="advice-dialog-title"
        tabIndex={-1}
        style={{ maxHeight: "85dvh" }}
        className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-t-card border border-line bg-surface text-fg shadow-xl outline-none sm:rounded-card"
      >
        <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-2">
          <h2 id="advice-dialog-title" className="text-base font-semibold">Setup notes</h2>
          <button type="button" onClick={onClose} aria-label="Close setup notes" className="flex h-11 w-11 shrink-0 items-center justify-center rounded-ctl text-fg hover:bg-surface-2">
            <span aria-hidden="true">✕</span>
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4 py-3">
          {groups.map((g) => (
            <section key={g.severity} aria-labelledby={`advice-group-${g.severity}`} className="mb-4 last:mb-0">
              <h3 id={`advice-group-${g.severity}`} className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted">
                <span aria-hidden="true" className={`h-2 w-2 rounded-full ${GROUP_DOT[g.severity]}`} />
                {g.label}
              </h3>
              <ul className="grid gap-2">
                {g.notes.map((n) => (
                  <li key={noteKey(n)} className="min-w-0 break-words rounded-ctl border border-line bg-surface-2 px-3 py-2 text-sm">
                    <p>{n.text}</p>
                    {n.action && <p className="mt-1 text-muted">{n.action}</p>}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
        {action && (
          <div className="border-t border-line px-4 py-3">
            <button type="button" onClick={action.onClick} className={secondaryButton}>
              {action.label}
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body
  );
}

// `notes`: what to offer (already filtered for the place). `fallbackFocus`: where focus goes after closing when the button has gone (it disappears once the notes are read).
export function SetupNotes({ notes, enabled = true, variant = "page", fallbackFocus, action }) {
  const read = useReadSet();
  const [shown, setShown] = useState(null); // the notes as they were when the dialog opened
  const opener = useRef(null);
  const shownRef = useRef(null);
  shownRef.current = shown;
  const fallbackRef = useRef(fallbackFocus);
  fallbackRef.current = fallbackFocus;
  const state = buttonState(notes, read);

  const close = useCallback(() => {
    if (!shownRef.current) return;
    store.markRead(shownRef.current.map(noteKey)); // what was shown counts as read, however the dialog was closed
    shownRef.current = null;
    setShown(null);
    const back = opener.current;
    setTimeout(() => {
      if (back && document.contains(back)) back.focus();
      else fallbackRef.current?.()?.focus?.();
    }, 0);
  }, []);

  const runAction = () => {
    action?.onClick?.();
    close();
  };

  if (!enabled) return null;
  return (
    <>
      {state.visible && !shown && (
        <button
          type="button"
          aria-haspopup="dialog"
          aria-label={`Setup notes, ${state.count}${state.alert ? ", including something that is not possible" : ""}`}
          onClick={(e) => {
            opener.current = e.currentTarget;
            setShown(notes);
          }}
          className="-my-3 inline-flex min-h-[44px] items-center"
        >
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${
              variant === "rail" ? "border-white/25 text-rail-text hover:bg-rail-active" : "border-line-strong text-muted hover:bg-surface-2 hover:text-fg"
            }`}
          >
            Setup notes <span className="font-semibold">{state.count}</span>
            {state.alert && <span aria-hidden="true" className="h-2 w-2 rounded-full bg-neg" />}
          </span>
        </button>
      )}
      {shown && <Dialog groups={groupNotes(shown)} action={action ? { label: action.label, onClick: runAction } : null} onClose={close} />}
    </>
  );
}
