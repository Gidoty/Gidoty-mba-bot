import { useState } from "react";

import { api } from "../api/client.js";
import { primaryButton, secondaryButton } from "../components/form.jsx";
import { Card, CardTitle, ErrorBanner, PageHeader, Pill } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";

const MESSAGES = [
  ["Entries", "When the bot opens a trade: side, pair, entry price, quantity and stop-loss."],
  ["Exits", "When a trade closes: why it closed, entry and exit price, and the result."],
  ["Safety exits", "If a stop-loss cannot be placed and the bot closes the position to protect you."],
  ["Attention alerts", "If an open position needs a safety check, and a note when it is resolved."],
  ["Daily summary", "How many trades closed that day and their combined result."],
];

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
    <div className="grid gap-4">
      <PageHeader title="Notifications" subtitle="Trade alerts and daily summaries." />
      <ErrorBanner message={error} />

      <Card>
        <CardTitle right={<Pill tone={linked ? "ok" : "warn"}>{linked ? "Linked" : "Not linked"}</Pill>}>Telegram</CardTitle>
        <p className="text-sm text-muted">
          {linked
            ? "The bot messages your linked Telegram chat. Relink if you want the messages to go to a different chat."
            : "The bot will not scan or trade until Telegram is linked, so it can send you trade entries, exits and a daily summary."}
        </p>

        {!linked && (
          <ol className="mt-4 list-decimal space-y-1.5 pl-5 text-sm text-fg">
            <li>Choose "Open Telegram and link". Telegram opens in a new tab.</li>
            <li>In Telegram, press Start.</li>
            <li>Come back here and choose "I've linked it - refresh".</li>
          </ol>
        )}

        <div className="mt-4 flex flex-wrap gap-3">
          <button type="button" onClick={getLink} className={linked ? secondaryButton : primaryButton}>
            {linked ? "Relink Telegram" : "Open Telegram and link"}
          </button>
          {linkUrl && (
            <button type="button" onClick={checkLinked} disabled={checking} className={primaryButton}>
              {checking ? "Checking..." : "I've linked it - refresh"}
            </button>
          )}
        </div>
      </Card>

      <div className="grid items-start gap-4 sm:grid-cols-2">
        <Card>
          <CardTitle>What you will receive</CardTitle>
          <dl className="space-y-3">
            {MESSAGES.map(([name, body]) => (
              <div key={name}>
                <dt className="text-sm font-semibold text-fg">{name}</dt>
                <dd className="text-sm text-muted">{body}</dd>
              </div>
            ))}
          </dl>
        </Card>

        <Card>
          <CardTitle right={<Pill tone="info">Example</Pill>}>What an entry message looks like</CardTitle>
          <div className="rounded-card rounded-tl-sm border border-line bg-surface-2 p-3.5 font-mono text-xs leading-relaxed text-fg">
            <p className="font-semibold">[LIVE] Entered LONG BTC/USDT</p>
            <p>Entry: 104250.0000 Qty: 0.079000</p>
            <p>Stop: 102980.0000 (resting at Binance)</p>
          </div>
          <p className="mt-3 text-xs text-muted">Invented numbers, shown only so you know what to expect. Practice accounts get the same kind of message marked PAPER.</p>
        </Card>
      </div>
    </div>
  );
}
