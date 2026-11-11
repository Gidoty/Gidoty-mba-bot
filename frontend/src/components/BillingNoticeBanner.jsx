import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { primaryButton } from "./form.jsx";
import { Notice } from "./ui.jsx";

// One billing notice at a time (renewal failed, card expiring, subscription ends soon, subscription cancelled). The backend
// decides what to say and whether a "Pay now" button is allowed; while subscription billing is switched off it always answers
// `{ notice: null }`, so this renders nothing. The wording lives in the backend so the Telegram message and this banner agree.
export default function BillingNoticeBanner() {
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    api
      .get("/billing/notice")
      .then((result) => {
        if (live) setNotice(result?.notice || null);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  async function payNow() {
    setBusy(true);
    setError(null);
    try {
      const { authorization_url } = await api.post("/billing/subscription/initialize");
      window.location.href = authorization_url;
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  if (!notice) return null;

  return (
    <div className="mb-4">
      <Notice>
        <p>{notice.message}</p>
        {error && (
          <p role="alert" className="mt-2 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2">
            {error}
          </p>
        )}
        {notice.action === "pay" && (
          <div className="mt-3">
            <button type="button" onClick={payNow} disabled={busy} className={primaryButton}>
              {busy ? "Opening payment..." : "Pay now"}
            </button>
          </div>
        )}
      </Notice>
    </div>
  );
}
