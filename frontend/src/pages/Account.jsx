import { Badge, Card, PageHeader } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";

export default function Account({ customer, onLogout }) {
  const { account } = useAccount();

  return (
    <div>
      <PageHeader title="Account" subtitle="Your profile." />

      <Card>
        <dl className="space-y-3 text-sm">
          <Row label="Email" value={customer.email} />
          <Row label="Member since" value={new Date(customer.created_at).toLocaleDateString()} />
          <Row label="Bot code" value={account?.bot_code || "-"} />
          <Row label="Account status" value={<Badge tone={customer.has_paid ? "accent" : "amber"}>{customer.has_paid ? "Activated" : "Not activated"}</Badge>} />
        </dl>
      </Card>

      <button
        onClick={onLogout}
        className="mt-4 w-full rounded-md border border-slate-300 bg-white px-4 py-2.5 text-sm font-medium text-ink hover:bg-slate-50"
      >
        Log out
      </button>
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-100 pb-3 last:border-0 last:pb-0">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-medium text-ink">{value}</dd>
    </div>
  );
}
