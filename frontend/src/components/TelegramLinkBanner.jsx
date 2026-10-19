import { useState } from "react";

import { api } from "../api/client.js";
import { useAccount } from "../context/AccountContext.jsx";

// Shown on every authenticated page while the account has no linked
// Telegram chat. trader.py skips an account with no notification_target
// entirely, so until this is done the bot does not scan or trade - the
// dashboard itself is reachable straight after signup, but practice
// trading only starts once this step is complete.
export default function TelegramLinkBanner() {
  const { refreshAccount } = useAccount();
  const [linkUrl, setLinkUrl] = useState(null);
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);

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
    }
  }

  return (
    <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 p-4">
      <p className="text-sm font-semibold text-amber-900">Link Telegram to start practice trading</p>
      <p className="mt-1 text-sm text-amber-900">
        Your account is ready. The bot will not scan or trade until Telegram is linked, so we can send you trade
        entries, exits, and daily summaries.
      </p>
      {error && <p className="mt-2 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
      <div className="mt-3 flex flex-wrap gap-3">
        <button
          onClick={getLink}
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark"
        >
          Open Telegram and link
        </button>
        {linkUrl && (
          <button
            onClick={checkLinked}
            disabled={checking}
            className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50 disabled:opacity-60"
          >
            {checking ? "Checking..." : "I've linked it - continue"}
          </button>
        )}
      </div>
    </div>
  );
}
