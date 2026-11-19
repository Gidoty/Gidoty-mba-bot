import { SubscriptionStatus } from "./SubscribeCard.jsx";
import useBillingOptions from "./useBillingOptions.js";
import { screenKind } from "../lib/billing.js";

// Dashboard: the state of a subscriber (active, grace, cancelled). Nothing while subscription billing is off, for a lifetime customer, for a new or expired customer (the Activate offer handles them), or on any error.
export default function SubscriptionStatusCard() {
  const billing = useBillingOptions();
  if (billing.status !== "ready" || screenKind(billing.options) !== "status") return null;
  return <SubscriptionStatus options={billing.options} />;
}
