import { useState } from "react";

import { api } from "../api/client.js";
import { Badge, Card, ErrorBanner, PageHeader } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";

export default function Notifications() {
  const { account, refreshAccount } = useAccount();
  const [linkUrl, setLinkUrl] = useState(null);
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);
  const linked = Boolean(account.notification_target);

  async function getLink() {
    setError(null);
    try {
      const { link_url } = await api.get("/telegram/link-url");
      setLinkUrl(link_url);
      window.open(link_url, "_blank", "noopener");
    } catch (err) {
      setError(err.message);
    }
  }

  async function checkLinked() {
    setChecking(true);
    try {
      await refreshAccount();
    } finally {
      setChecking(false);
      setLinkUrl(null);
    }
  }

  return (
    <div>
      <PageHeader title="Notifications" subtitle="Trade alerts and daily summaries." />
      <ErrorBanner message={error} />

      <Card>
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium text-ink">Telegram</p>
          <Badge tone={linked ? "accent" : "amber"}>{linked ? "Linked" : "Not linked"}</Badge>
        </div>
        <p className="mt-1 text-sm text-slate-600">
          The bot messages this chat whenever it opens or closes a trade, plus a daily P&L summary.
        </p>

        <div className="mt-4 flex flex-wrap gap-3">
          <button
            onClick={getLink}
            className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50"
          >
            {linked ? "Relink Telegram" : "Open Telegram and link"}
          </button>
          {linkUrl && (
            <button
              onClick={checkLinked}
              disabled={checking}
              className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark disabled:opacity-60"
            >
              {checking ? "Checking..." : "I've linked it - refresh"}
            </button>
          )}
        </div>
      </Card>
    </div>
  );
}
