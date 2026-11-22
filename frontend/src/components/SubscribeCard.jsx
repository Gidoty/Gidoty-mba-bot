import { useState } from "react";

import { api } from "../api/client.js";
import { CANCEL_WORDING, channels, renewView, stateView } from "../lib/billing.js";
import { primaryButton, secondaryButton } from "./form.jsx";
import { Card } from "./ui.jsx";

// The pay buttons for the channels the server says may start now. A click asks the matching checkout route (card: subscription, crypto: crypto invoice) and goes to the address it returns.
export function PayButtons({ options }) {
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const list = channels(options);
  async function go(c) {
    setBusy(c.kind);
    setError(null);
    try {
      const { authorization_url } = await api.post(c.path);
      window.location.href = authorization_url;
    } catch (err) {
      setError(err.message);
      setBusy(null);
    }
  }
  if (list.length === 0) return null;
  return (
    <div>
      {error && (
        <p role="alert" className="mb-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm text-fg">
          {error}
        </p>
      )}
      <ul className="grid gap-3">
        {list.map((c, i) => (
          <li key={c.kind} className="min-w-0">
            <button type="button" onClick={() => go(c)} disabled={busy !== null} className={`${i === 0 ? primaryButton : secondaryButton} w-full sm:w-auto`}>
              {busy === c.kind ? "Opening payment..." : c.price ? `${c.label}: ${c.price} a month` : c.label}
            </button>
            {c.note && <p className="mt-1 text-xs text-muted">{c.note}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

// The Activate offer for a new customer (and for one whose subscription has ended). Prices and channels come from the server; the cancel wording is the Terms of Service sentence.
export default function SubscribeCard({ options }) {
  const available = channels(options).length > 0;
  const renew = renewView(options); // a lapsed customer: Renew wording and the lapse date, not the first-time offer
  return (
    <div className="rounded-card border border-line border-l-4 border-l-brand-line bg-surface p-5">
      <p className="text-sm font-semibold text-fg">{renew ? renew.title : "Ready to move beyond practice trading?"}</p>
      {renew && <p className="mt-1 text-sm text-fg">{renew.body}</p>}
      <p className="mt-1 text-sm text-muted">
        {renew
          ? "Renewing starts a new monthly period. Real funds stay under your control: renewing does not change your trading settings."
          : "A monthly subscription unlocks the full trading environment. You'll still choose, separately and explicitly, if and when to connect real funds - subscribing does not do that by itself."}
      </p>
      <div className="mt-4">
        {available ? <PayButtons options={options} /> : <p className="text-sm text-muted">{renew ? "Renewal is not available right now. Please check back soon." : "Monthly subscriptions are not available yet. Please check back soon."}</p>}
      </div>
      <p className="mt-4 text-xs text-muted">{CANCEL_WORDING}</p>
    </div>
  );
}

// A subscriber's state on the dashboard (active, grace, cancelled). The billing banner above it keeps showing its own reminder text; in grace this card also offers the pay buttons.
export function SubscriptionStatus({ options }) {
  const view = stateView(options);
  if (!view) return null;
  return (
    <Card className="mb-4">
      <p className="text-sm font-semibold text-fg">{view.title}</p>
      <p className="mt-1 text-sm text-muted">{view.body}</p>
      {view.kind === "grace" && (
        <div className="mt-3">
          <PayButtons options={options} />
        </div>
      )}
    </Card>
  );
}
