import { Card } from "./ui.jsx";
import { secondaryButton } from "./form.jsx";
import useBillingOptions from "./useBillingOptions.js";
import { screenKind, stateView } from "../lib/billing.js";
import { DASHBOARD_LINK_TEXT, SUBSCRIPTIONS_ROUTE, dateLine } from "../lib/subscriptions.js";
import { navigate } from "../useHashRoute.js";

// Dashboard: the state of a subscriber (active, grace, cancelled) as a short card with a link to Subscriptions, where the renewal buttons are. Nothing while subscription billing is off, for a lifetime customer, for a new or
// expired customer (their Dashboard line points to Subscriptions), or on any error.
export default function SubscriptionStatusCard() {
  const billing = useBillingOptions();
  if (billing.status !== "ready" || screenKind(billing.options) !== "status") return null;
  const view = stateView(billing.options);
  if (!view) return null;
  const date = dateLine(billing.options);
  return (
    <Card className="mb-4">
      <p className="text-sm font-semibold text-fg">{view.title}</p>
      <p className="mt-1 text-sm text-muted">{view.body}</p>
      {date && !view.body.includes(date.replace(/\.$/, "")) && <p className="mt-1 text-sm text-fg">{date}</p>}
      <button type="button" onClick={() => navigate(SUBSCRIPTIONS_ROUTE)} className={`${secondaryButton} mt-3 w-full sm:w-auto`}>
        {DASHBOARD_LINK_TEXT}
      </button>
    </Card>
  );
}
