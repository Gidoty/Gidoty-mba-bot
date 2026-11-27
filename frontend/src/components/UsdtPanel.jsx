import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client.js";
import { NEW_INVOICE_LABEL, POLL_SECONDS, clockOffsetMs, panelView, shouldResume } from "../lib/usdtPanel.js";
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

// The panel for one USDT payment: the stages the payment provider reported, a bold box saying exactly what to pay, and a new invoice when the provider says the old one expired. No countdown of our own: the provider's payment page has one.
export default function UsdtPanel({ invoiceUrl, onNewInvoice, newInvoiceBusy = false, newInvoiceError = null }) {
  const { status, failed, reload } = useCryptoStatus(true);
  useEffect(() => {
    reload(); // a new invoice was just created: read it now instead of waiting for the next poll
  }, [invoiceUrl, reload]);

  const view = panelView(status);
  if (!view) {
    return (
      <div className="rounded-ctl border border-line bg-surface p-4" role="status">
        <p className="text-sm text-muted">{failed ? "We could not read your payment status. Retrying..." : "Loading your payment status..."}</p>
      </div>
    );
  }
  return (
    <section aria-label="USDT payment" className="rounded-ctl border border-line bg-surface p-4">
      <h3 className="text-sm font-semibold text-fg">{view.headline}</h3>
      {view.detail && <p className="mt-1 text-sm text-muted">{view.detail}</p>}
      {view.showReference && (
        <p className="mt-2 text-sm text-fg">
          Reference for support: <span className="select-all break-all font-mono">{view.reference}</span>
        </p>
      )}
      {view.assetBox && (
        <div role="note" aria-label="What to pay" className="mt-3 rounded-ctl border-2 border-warn bg-warn-soft p-4 text-fg">
          <p className="text-xl font-bold leading-snug">{view.assetBox.heading}</p>
          {view.assetBox.amount && <p className="mt-1 text-base font-semibold">{view.assetBox.amount}</p>}
          <p className="mt-2 text-sm">{view.assetBox.warning}</p>
        </div>
      )}
      {!view.terminal && invoiceUrl && view.stage === "waiting" && (
        <p className="mt-3">
          <a href={invoiceUrl} target="_blank" rel="noopener noreferrer" className={`${secondaryButton} w-full sm:w-auto`}>
            Open the payment page
          </a>
        </p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        {view.showNewInvoice && (
          <button type="button" onClick={onNewInvoice} disabled={newInvoiceBusy} className={`${primaryButton} w-full sm:w-auto`}>
            {newInvoiceBusy ? "Creating..." : NEW_INVOICE_LABEL}
          </button>
        )}
      </div>
      <div aria-live="polite" className="mt-2">
        {newInvoiceError && (
          <p role="alert" className="text-sm text-neg">
            {newInvoiceError}
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
