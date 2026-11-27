import { useState } from "react";

import { api } from "../api/client.js";
import { primaryButton } from "../components/form.jsx";
import SubscribeCard, { PayButtons } from "../components/SubscribeCard.jsx";
import { Card, PageHeader } from "../components/ui.jsx";
import useBillingOptions from "../components/useBillingOptions.js";
import { CANCEL_WORDING } from "../lib/billing.js";
import { subscriptionsView } from "../lib/subscriptions.js";

// The one place where an account is activated or renewed: the subscription state, the renewal or expiry date, and the payment options the server says may start now (card through Paystack, USDT through
// NOWPayments). Nothing here decides who may pay: it shows what GET /api/billing/options says and calls the same checkout routes the Dashboard called. There is no payment history here: no endpoint returns one.
export default function Subscriptions({ customer }) {
  const billing = useBillingOptions(); // 404 (subscription billing off) means "off": the old one-time activation
  const view = subscriptionsView(billing, customer);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function activate() {
    setBusy(true);
    setError(null);
    try {
      const { authorization_url } = await api.post("/billing/initialize");
      window.location.href = authorization_url;
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  if (view.kind === "loading") {
    return (
      <div className="mx-auto grid max-w-3xl gap-4">
        <PageHeader title="Subscriptions" subtitle="Your subscription and how to pay for it." />
        <p className="text-sm text-muted">Loading...</p>
      </div>
    );
  }

  return (
    <div className="mx-auto grid max-w-3xl gap-4">
      <PageHeader title="Subscriptions" subtitle="Your subscription and how to pay for it." />
      {view.pay !== "offer" && (
        <Card>
          <p className="text-sm font-semibold text-fg">{view.title}</p>
          {view.body && <p className="mt-1 text-sm text-muted">{view.body}</p>}
          {view.dateLine && <p className="mt-1 text-sm text-fg">{view.dateLine}</p>}
          {view.pay === "renew" && (
            <div className="mt-3">
              <PayButtons options={billing.options} />
              <p className="mt-3 text-xs text-muted">{CANCEL_WORDING}</p>
            </div>
          )}
          {view.pay === "legacy" && (
            <div className="mt-3">
              {error && (
                <p role="alert" className="mb-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm text-fg">
                  {error}
                </p>
              )}
              <button type="button" onClick={activate} disabled={busy} className={`${primaryButton} w-full sm:w-auto`}>
                {busy ? "Opening payment..." : "Activate"}
              </button>
            </div>
          )}
        </Card>
      )}
      {view.pay === "offer" && (
        <>
          {view.dateLine && <p className="text-sm text-fg">{view.dateLine}</p>}
          <SubscribeCard options={billing.options} />
        </>
      )}
    </div>
  );
}
