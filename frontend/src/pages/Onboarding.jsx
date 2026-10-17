import { useState } from "react";

import { api } from "../api/client.js";
import HelpTip from "../components/HelpTip.jsx";
import { PAIRS, UPCOMING_PAIRS } from "../constants.js";

const SUPPORT_WHATSAPP_NUMBER = "2348124228730"; // 0812 422 8730, in wa.me's international-digits-only format
const SUPPORT_EMAIL = "support@metabridgeacademy.com";

export default function Onboarding({ account, onLogout, onProgress }) {
  if (!account) {
    return <AccountForm onLogout={onLogout} onCreated={onProgress} />;
  }
  return <TelegramLinkStep account={account} onLogout={onLogout} onLinked={onProgress} />;
}

function Shell({ title, onLogout, children }) {
  return (
    <div className="min-h-screen bg-slate-50 px-4 py-10">
      <div className="mx-auto max-w-lg">
        <div className="mb-6 flex items-center justify-between">
          <span className="text-lg font-semibold text-ink">MBA Bot</span>
          <button onClick={onLogout} className="text-sm text-slate-500 hover:text-ink">
            Log out
          </button>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-8 shadow-sm">
          <h1 className="mb-6 text-xl font-semibold text-ink">{title}</h1>
          {children}
        </div>
      </div>
    </div>
  );
}

function AccountForm({ onLogout, onCreated }) {
  const [pair, setPair] = useState(PAIRS[0]);
  const [marketType, setMarketType] = useState("spot");
  const [riskPct, setRiskPct] = useState("1");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.post("/account", {
        pair,
        market_type: marketType,
        notification_channel: "telegram",
        risk_pct: riskPct ? Number(riskPct) : undefined,
        api_key: apiKey,
        api_secret: apiSecret,
      });
      onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Shell title="Set up your trading account" onLogout={onLogout}>
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            Pair
            <HelpTip text="The crypto pair the bot watches and trades, e.g. BTC/USDT means it trades Bitcoin priced in Tether. Pick one you're comfortable with - you can't change this later without creating a new account." />
          </span>
          <select
            value={pair}
            onChange={(e) => setPair(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          >
            {PAIRS.map((p) => (
              <option key={p}>{p}</option>
            ))}
            {UPCOMING_PAIRS.map((p) => (
              <option key={p} value={p} disabled>
                {p} - coming soon
              </option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            Market
            <HelpTip text="Spot: you buy and sell the actual coin, simplest and lowest risk. Futures: you trade with leverage, which can multiply both gains and losses. If you're not sure, choose Spot." />
          </span>
          <select
            value={marketType}
            onChange={(e) => setMarketType(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          >
            <option value="spot">Spot</option>
            <option value="futures">Futures</option>
          </select>
        </label>

        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            Risk per trade (%)
            <HelpTip text="How much of your balance the bot risks on a single trade before its stop-loss triggers. Lower means smaller, steadier moves; higher means bigger swings, up and down. Kept at 2% or less either way." />
          </span>
          <input
            type="number"
            min="0.1"
            max="2"
            step="0.1"
            value={riskPct}
            onChange={(e) => setRiskPct(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
          <span className="mt-1 block text-xs text-slate-500">Capped at 2% of your account balance per trade.</span>
        </label>

        <div className="rounded-md bg-accent/5 border border-accent/20 px-3 py-2 text-xs text-slate-700">
          <strong className="text-accent-dark">Your money never leaves Binance.</strong> This key only gives the
          bot permission to place trades on your behalf - your balance and your profits stay in your own Binance
          account the entire time. The bot cannot withdraw a single dollar: the key you create below must have
          withdrawals switched off, and we check that on our end too.
        </div>

        <HowToGetApiKey />

        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            Binance API key
            <HelpTip text={'A code Binance generates for you under API Management, so this bot can place trades on your behalf without needing your password. See "Don\'t know how to get a Binance API key?" above for the full walkthrough.'} />
          </span>
          <input
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            required
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono"
          />
        </label>

        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            Binance API secret
            <HelpTip text="Shown by Binance only once, right next to your API key when you create it. Copy both at the same time - if you leave that page first, you'll need to delete the key and make a new one." />
          </span>
          <input
            type="password"
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
            required
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono"
          />
        </label>

        <ContactSupport />

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-accent px-4 py-2 font-medium text-white hover:bg-accent-dark disabled:opacity-60"
        >
          {submitting ? "Creating..." : "Continue"}
        </button>
      </form>
    </Shell>
  );
}

function HowToGetApiKey() {
  return (
    <details className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
      <summary className="cursor-pointer font-medium text-ink">
        Don't know how to get a Binance API key? Click here - step by step
      </summary>
      <ol className="mt-3 list-decimal space-y-2 pl-5 text-slate-600">
        <li>Log in to your account at binance.com (or open the Binance app).</li>
        <li>
          Click your profile icon (top right on desktop, or the person icon on the app), then open{" "}
          <strong>API Management</strong>.
        </li>
        <li>
          Click <strong>Create API</strong>, choose "System generated", and give it any label - e.g. "MBA Bot".
          Binance will ask you to verify this with your password and/or 2FA.
        </li>
        <li>
          Once it's created, click <strong>Edit restrictions</strong> on the new key. Turn <strong>on</strong>{" "}
          "Enable Spot & Margin Trading" (or "Enable Futures" if you chose Futures above).{" "}
          <strong className="text-red-700">Leave "Enable Withdrawals" switched off</strong> - this is what keeps
          your funds safe even if this key were ever exposed.
        </li>
        <li>
          Binance shows you the <strong>API Key</strong> and <strong>Secret Key</strong> once, right after
          creation. Copy both immediately and paste them into the two fields below - if you navigate away before
          copying the secret, you'll have to delete the key and create a new one.
        </li>
      </ol>
    </details>
  );
}

function ContactSupport() {
  if (!SUPPORT_WHATSAPP_NUMBER && !SUPPORT_EMAIL) return null;
  return (
    <p className="text-xs text-slate-500">
      Still stuck? MetaBridge Academy can help you set this up.
      {SUPPORT_WHATSAPP_NUMBER && (
        <>
          {" "}
          WhatsApp{" "}
          <a
            href={`https://wa.me/${SUPPORT_WHATSAPP_NUMBER}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent hover:underline"
          >
            {SUPPORT_WHATSAPP_NUMBER}
          </a>
          .
        </>
      )}
      {SUPPORT_EMAIL && (
        <>
          {" "}
          Email{" "}
          <a href={`mailto:${SUPPORT_EMAIL}`} className="text-accent hover:underline">
            {SUPPORT_EMAIL}
          </a>
          .
        </>
      )}
    </p>
  );
}

function TelegramLinkStep({ account, onLogout, onLinked }) {
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
      await onLinked();
    } finally {
      setChecking(false);
    }
  }

  return (
    <Shell title="Connect Telegram" onLogout={onLogout}>
      {account?.bot_code && (
        <p className="mb-4 inline-block rounded-full bg-slate-100 px-3 py-1 font-mono text-xs text-slate-600">
          Bot code: {account.bot_code}
        </p>
      )}
      <p className="text-sm text-slate-600">
        Your account is created with a free, unlimited paper trading trial - no card needed, no real funds at risk.
        Last step: link Telegram so we can send you trade entries, exits, and daily P&L summaries. The bot won't
        trade at all, paper or live, until this is done.
      </p>
      {error && <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
      <div className="mt-6 space-y-3">
        <button
          onClick={getLink}
          className="w-full rounded-md bg-accent px-4 py-2 font-medium text-white hover:bg-accent-dark"
        >
          Open Telegram and link
        </button>
        {linkUrl && (
          <button
            onClick={checkLinked}
            disabled={checking}
            className="w-full rounded-md border border-slate-300 px-4 py-2 font-medium text-ink hover:bg-slate-50 disabled:opacity-60"
          >
            {checking ? "Checking..." : "I've linked it - continue"}
          </button>
        )}
      </div>
    </Shell>
  );
}
