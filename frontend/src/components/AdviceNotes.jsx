import { useEffect, useRef, useState } from "react";

import { api } from "../api/client.js";
import { SEVERITY_EDGE, SEVERITY_LABEL, notesForTab, requestKey } from "../lib/advice.js";

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

// The notes for one tab. Nothing at all when the advisor is off or has nothing to say.
export default function AdviceNotes({ advice, tab }) {
  const notes = notesForTab(advice?.notes, tab);
  if (!advice?.enabled || notes.length === 0) return null;
  return (
    <section aria-label="Setup notes" className="mt-4 grid gap-2">
      <h3 className="text-sm font-semibold text-fg">Setup notes</h3>
      <ul className="grid gap-2">
        {notes.map((n) => (
          <li key={n.id} className={`rounded-ctl border border-line border-l-4 px-3 py-2 text-sm text-fg ${SEVERITY_EDGE[n.severity] || SEVERITY_EDGE.tip}`}>
            <span className="font-semibold">{SEVERITY_LABEL[n.severity] || "Good to know"}: </span>
            {n.text}
            {n.action && <span className="mt-1 block text-muted">{n.action}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}
