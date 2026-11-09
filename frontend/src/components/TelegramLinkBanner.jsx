import { useState } from "react";

import { api } from "../api/client.js";
import { useAccount } from "../context/AccountContext.jsx";
import { primaryButton, secondaryButton } from "./form.jsx";
import { Notice } from "./ui.jsx";

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
    <div className="mb-4">
      <Notice>
        <p className="font-semibold">Link Telegram to start practice trading</p>
        <p className="mt-1">
          Your account is ready. The bot will not scan or trade until Telegram is linked, so we can send you trade
          entries, exits, and daily summaries.
        </p>
        {error && (
          <p role="alert" className="mt-2 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2">
            {error}
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-3">
          <button type="button" onClick={getLink} className={primaryButton}>
            Open Telegram and link
          </button>
          {linkUrl && (
            <button type="button" onClick={checkLinked} disabled={checking} className={secondaryButton}>
              {checking ? "Checking..." : "I've linked it - continue"}
            </button>
          )}
        </div>
      </Notice>
    </div>
  );
}
