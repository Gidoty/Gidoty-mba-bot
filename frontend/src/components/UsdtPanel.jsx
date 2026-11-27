import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../api/client.js";
import { CHECK_COOLDOWN_SECONDS, NEW_INVOICE_LABEL, POLL_SECONDS, SENT_LABEL, checkErrorMessage, checkMessage, clockOffsetMs, panelView, shouldResume } from "../lib/usdtPanel.js";
import { primaryButton, secondaryButton } from "./form.jsx";

// Reads the state of the customer's latest USDT invoice from OUR database (GET /api/billing/crypto/status: no call to the payment provider) now and then every POLL_SECONDS while `enabled`.
export function useCryptoStatus(enabled) {
  const [state, setState] = useState({ status: null, offset: 0, failed: false });
  const alive = useRef(true);
  const load = useCallback(async () => {
    try {
      const status = await api.get("/billing/crypto/status");
      if (alive.current) setState({ status, offset: clockOffsetMs(status.server_time, Date.now()), failed: false });
    } catch {
      if (alive.current) setState((s) => ({ ...s, failed: true }));
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    if (!enabled) return undefined;
    load();
    const id = setInterval(load, POLL_SECONDS * 1000);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [enabled, load]);
  return { ...state, reload: load };
}

// One tick per second, only so the displayed countdown moves. The end time itself comes from the server and never changes.
function useNow() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

// The panel for one USDT payment: plain stages, the countdown to the invoice's fixed end, the "I have sent the payment" button (it grants nothing by itself: the server asks the provider and applies its usual rule), and a
// new invoice when the old one has expired. The countdown never starts again by itself.
export default function UsdtPanel({ invoiceUrl, onNewInvoice, newInvoiceBusy = false, newInvoiceError = null }) {
  const { status, offset, failed, reload } = useCryptoStatus(true);
  const now = useNow();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [cooldownUntil, setCooldownUntil] = useState(0);
  useEffect(() => {
    reload(); // a new invoice was just created: read it now instead of waiting for the next poll
  }, [invoiceUrl, reload]);

  const view = panelView(status, now, offset);
  if (!view) {
    return (
      <div className="rounded-ctl border border-line bg-surface p-4" role="status">
        <p className="text-sm text-muted">{failed ? "We could not read your payment status. Retrying..." : "Loading your payment status..."}</p>
      </div>
    );
  }
  const waitingSeconds = Math.max(0, Math.ceil((cooldownUntil - now) / 1000));

  async function sent() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const answer = await api.post("/billing/crypto/check", { reference: view.reference });
      setMessage(checkMessage(answer));
      setCooldownUntil(Date.now() + CHECK_COOLDOWN_SECONDS * 1000);
      reload();
    } catch (err) {
      setError(checkErrorMessage(err instanceof ApiError ? err.status : 0, err instanceof ApiError && err.status >= 500 ? err.message : null));
      if (err instanceof ApiError && err.status === 429) setCooldownUntil(Date.now() + CHECK_COOLDOWN_SECONDS * 1000);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="USDT payment" className="rounded-ctl border border-line bg-surface p-4">
      <h3 className="text-sm font-semibold text-fg">{view.headline}</h3>
      {view.detail && <p className="mt-1 text-sm text-muted">{view.detail}</p>}
      {view.instructions && <p className="mt-2 text-sm text-fg">{view.instructions}</p>}
      {view.countdown && (
        <p className="mt-2 text-sm text-fg" aria-live="off">
          Time left to send the payment: <span className="font-mono font-semibold">{view.countdown}</span>
        </p>
      )}
      {view.showReference && (
        <p className="mt-2 text-sm text-fg">
          Reference for support: <span className="select-all break-all font-mono">{view.reference}</span>
        </p>
      )}
      {!view.terminal && invoiceUrl && view.stage === "waiting" && view.secondsLeft > 0 && (
        <p className="mt-3">
          <a href={invoiceUrl} target="_blank" rel="noopener noreferrer" className={`${secondaryButton} w-full sm:w-auto`}>
            Open the payment page
          </a>
        </p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        {view.showSent && (
          <button type="button" onClick={sent} disabled={busy || waitingSeconds > 0} className={`${primaryButton} w-full sm:w-auto`}>
            {busy ? "Checking..." : waitingSeconds > 0 ? `${SENT_LABEL} (wait ${waitingSeconds}s)` : SENT_LABEL}
          </button>
        )}
        {view.showNewInvoice && (
          <button type="button" onClick={onNewInvoice} disabled={newInvoiceBusy} className={`${view.showSent ? secondaryButton : primaryButton} w-full sm:w-auto`}>
            {newInvoiceBusy ? "Creating..." : NEW_INVOICE_LABEL}
          </button>
        )}
      </div>
      <div aria-live="polite" className="mt-2">
        {message && <p className="text-sm text-fg">{message}</p>}
        {(error || newInvoiceError) && (
          <p role="alert" className="text-sm text-neg">
            {error || newInvoiceError}
          </p>
        )}
      </div>
      {failed && <p className="mt-2 text-xs text-muted">We could not refresh the status just now. We will keep trying.</p>}
    </section>
  );
}

// Whether to open the panel by itself when the page loads: the customer reloaded in the middle of a payment.
export function useResumeUsdt(enabled) {
  const [resume, setResume] = useState(false);
  useEffect(() => {
    if (!enabled) return undefined;
    let live = true;
    api.get("/billing/crypto/status").then(
      (status) => live && setResume(shouldResume(status, Date.now(), clockOffsetMs(status.server_time, Date.now()))),
      () => {}
    );
    return () => {
      live = false;
    };
  }, [enabled]);
  return resume;
}
