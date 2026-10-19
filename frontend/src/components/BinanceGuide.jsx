import { useEffect, useState } from "react";

import { BINANCE_GUIDE_SUPPORT, METABRIDGE_SERVER_IP } from "../constants.js";

// The "Need help connecting Binance?" entry point plus the step-by-step
// guide it opens. The guide is a modal rendered on top of the page, so a
// customer reading it never loses a half-filled credential form behind it
// (the form's state lives in the parent and the parent stays mounted).
// Purely presentational: it never touches credentials or the API.
export default function BinanceGuide({ className = "" }) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <div className={`flex flex-wrap items-center justify-between gap-2 rounded-lg border border-accent/30 bg-accent/5 px-4 py-3 ${className}`}>
        <p className="text-sm font-medium text-ink">Need help connecting Binance?</p>
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent-dark"
        >
          View Step-by-Step Guide
        </button>
      </div>
      {open && <GuideModal onClose={() => setOpen(false)} />}
    </>
  );
}

function GuideModal({ onClose }) {
  useEffect(() => {
    function onKey(e) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center sm:p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Binance connection guide"
        className="flex max-h-[92vh] w-full max-w-2xl flex-col rounded-t-2xl bg-white shadow-xl sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div>
            <h2 className="font-serif text-lg font-bold text-ink">Connect Binance: step by step</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              About 5 minutes. Your form stays exactly as you left it.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close guide"
            className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-100 hover:text-ink"
          >
            ✕
          </button>
        </div>

        <div className="space-y-5 overflow-y-auto px-5 py-5 text-sm text-slate-600">
          <Step n={1} title="Open API Management on Binance">
            <p>
              Sign in to Binance (website or app), open your profile, and go to <strong>API Management</strong>{" "}
              (also called API Key Management).
            </p>
            <Callout tone="red" title={'Do not create an "AI Payment Agent" API key'}>
              Metabridge Bot requires a standard Binance trading API key. Other Binance API products can give you
              legitimate-looking credentials, but those credentials will not necessarily work with Binance Spot or
              Futures trading, and the connection will fail.
            </Callout>
          </Step>

          <Step n={2} title="Create the API key">
            <p>
              Click <strong>Create API</strong>, choose the standard (system-generated) trading API key, and give it
              a label such as <em>Metabridge Bot</em>. Complete Binance's security check.
            </p>
            <p className="mt-2">
              One Binance API key can be used by Metabridge Bot for <strong>both Spot and Futures</strong>, as long
              as the permissions in Step 3 are enabled.
            </p>
            <Callout tone="amber" title="Keep your Secret Key private">
              Enter it only into the secure Metabridge Bot connection form. Never send your API Key or Secret Key to
              Metabridge Academy support, WhatsApp, Telegram, email, developers, or anyone else.
            </Callout>
          </Step>

          <Step n={3} title="Set the permissions">
            <p>Click <strong>Edit restrictions</strong> on the new key and match this table:</p>
            <PermissionTable />
            <p className="mt-2 text-xs text-slate-500">
              Wording can differ slightly between Binance app versions. Anything not listed should stay OFF.
            </p>
            <Callout tone="red" title="NEVER ENABLE WITHDRAWALS">
              Metabridge Bot does not require permission to withdraw your cryptocurrency. Withdrawal and unrelated
              transfer permissions should remain disabled.
            </Callout>
          </Step>

          <Step n={4} title="Restrict the key to Metabridge Bot's IP address">
            <p>
              Under IP access, choose <strong>Restrict access to trusted IPs only</strong>. Do <strong>not</strong>{" "}
              choose <span className="whitespace-nowrap">"Unrestricted (Less Secure)"</span>. Binance may keep
              trading permissions unavailable until trusted-IP protection is configured.
            </p>
            <p className="mt-2">Paste this Metabridge Bot server IPv4 address into Binance's trusted-IP field:</p>
            <CopyIp ip={METABRIDGE_SERVER_IP} />
            <p className="mt-2">
              This lets Binance accept requests using your API key from the authorized Metabridge Bot server while
              reducing exposure if the credentials are ever compromised elsewhere.
            </p>
          </Step>

          <Step n={5} title="Save on Binance">
            <p>Complete Binance's security verification and save the configuration. The recommended final setup is:</p>
            <p className="mt-2 rounded-md bg-slate-100 px-3 py-2 font-medium text-ink">
              Reading + Spot Trading + Futures Trading + Metabridge Bot's trusted IP, with withdrawals and transfers
              OFF.
            </p>
          </Step>

          <Step n={6} title="Return to Metabridge Bot">
            <p>
              Enter the new <strong>API Key</strong> and <strong>Secret Key</strong> in the connection form. The
              Secret Key stays masked, and neither value is shown again after you submit it. Binance shows the
              secret only once, so copy it before leaving that page.
            </p>
          </Step>

          <Step n={7} title="Test the connection before enabling Live Trading">
            <p>
              Use <strong>Verify Connection</strong> (first-time setup) or <strong>Test Connection</strong> (under
              Manage Connection) and wait for <strong>"Connection verified."</strong>
            </p>
            <Callout tone="amber" title="Saving a key is not proof that Binance accepted it">
              Only a successful test confirms Binance accepted your key. If it fails, check that the key is a
              standard trading key, that Reading and Trading are on, and that the trusted IP above was saved.
            </Callout>
          </Step>

          <SupportCard />
        </div>

        <div className="border-t border-slate-200 px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            className="w-full rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50"
          >
            Close guide
          </button>
        </div>
      </div>
    </div>
  );
}

function Step({ n, title, children }) {
  return (
    <section className="flex gap-3">
      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-bold text-white">
        {n}
      </span>
      <div className="min-w-0 flex-1">
        <h3 className="text-base font-semibold text-ink">{title}</h3>
        <div className="mt-1.5">{children}</div>
      </div>
    </section>
  );
}

const CALLOUT_TONES = {
  red: "border-red-300 bg-red-50 text-red-900",
  amber: "border-amber-300 bg-amber-50 text-amber-900",
};

function Callout({ tone, title, children }) {
  return (
    <div className={`mt-3 rounded-md border px-3 py-2.5 ${CALLOUT_TONES[tone]}`}>
      <p className="text-sm font-bold">
        {tone === "red" ? "⚠ " : "🔒 "}
        {title}
      </p>
      <p className="mt-0.5 text-sm">{children}</p>
    </div>
  );
}

const PERMISSIONS = [
  ["Enable Reading", true],
  ["Enable Spot & Margin & Stock Trading", true],
  ["Enable Margin Loan, Repay & Transfer", false],
  ["Enable Futures", true],
  ["Permits Universal Transfer", false],
  ["Enable Withdrawals", false],
  ["Enable Alpha Withdrawals", false],
  ["Enable Prediction Trading", false],
  ["Enable Symbol Whitelist", false, "unless Metabridge Bot explicitly supports it later"],
];

function PermissionTable() {
  return (
    <div className="mt-2 overflow-hidden rounded-md border border-slate-200">
      <table className="w-full text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2 font-semibold">Binance permission</th>
            <th className="px-3 py-2 font-semibold">Setting</th>
          </tr>
        </thead>
        <tbody>
          {PERMISSIONS.map(([name, on, note]) => (
            <tr key={name} className="border-t border-slate-100">
              <td className="px-3 py-2 text-ink">{name}</td>
              <td className={`px-3 py-2 font-semibold ${on ? "text-emerald-700" : "text-red-700"}`}>
                {on ? "✅ ON" : "❌ OFF"}
                {note && <span className="block text-xs font-normal text-slate-500">{note}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CopyIp({ ip }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(ip);
    } catch {
      // Older mobile browsers / non-secure contexts: fall back to a hidden textarea.
      const el = document.createElement("textarea");
      el.value = ip;
      el.setAttribute("readonly", "");
      el.style.position = "fixed";
      el.style.opacity = "0";
      document.body.appendChild(el);
      el.select();
      try {
        document.execCommand("copy");
      } finally {
        document.body.removeChild(el);
      }
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <code className="rounded-md bg-ink px-3 py-2 font-mono text-base font-semibold tracking-wide text-white">{ip}</code>
      <button
        type="button"
        onClick={copy}
        className="rounded-md border border-accent px-3 py-2 text-sm font-semibold text-accent-dark hover:bg-accent/10"
      >
        {copied ? "Copied ✓" : "Copy IP Address"}
      </button>
    </div>
  );
}

function WhatsAppIcon() {
  return (
    <svg viewBox="0 0 32 32" className="h-5 w-5" aria-hidden="true">
      <circle cx="16" cy="16" r="16" fill="#25D366" />
      <path
        fill="#fff"
        d="M16 6.5a9.4 9.4 0 0 0-8.1 14.2L6.6 25.5l4.9-1.3A9.4 9.4 0 1 0 16 6.5Zm0 1.8a7.6 7.6 0 1 1-3.9 14.1l-.3-.2-2.9.8.8-2.8-.2-.3A7.6 7.6 0 0 1 16 8.3Zm-3 3.5c-.2 0-.5.1-.7.4-.3.3-1 1-1 2.4s1 2.8 1.2 3c.1.2 2 3.1 4.9 4.2 2.4.9 2.9.7 3.4.7.5-.1 1.7-.7 1.9-1.4.2-.7.2-1.2.2-1.4l-.5-.3-1.8-.9c-.2-.1-.4-.1-.6.1l-.8 1c-.1.2-.3.2-.5.1-.3-.1-1.1-.4-2.1-1.3-.8-.7-1.3-1.5-1.4-1.8-.2-.3 0-.4.1-.5l.4-.5.3-.4v-.5l-.8-1.9c-.2-.5-.4-.4-.6-.4h-.5Z"
      />
    </svg>
  );
}

function SupportCard() {
  const { whatsappNumber, whatsappDisplay, email, whatsappMessage } = BINANCE_GUIDE_SUPPORT;
  const whatsappUrl = `https://wa.me/${whatsappNumber}?text=${encodeURIComponent(whatsappMessage)}`;
  return (
    <section className="rounded-lg border border-slate-200 bg-slate-50 p-4">
      <h3 className="text-base font-semibold text-ink">Still having trouble?</h3>
      <p className="mt-1">
        If you're unable to complete your Binance connection, our support team can help you through the setup.{" "}
        <strong>Never send your API Secret Key to support.</strong>
      </p>
      <dl className="mt-3 space-y-1">
        <div>
          <dt className="inline font-medium text-ink">WhatsApp Support: </dt>
          <dd className="inline">{whatsappDisplay}</dd>
        </div>
        <div>
          <dt className="inline font-medium text-ink">Email Support: </dt>
          <dd className="inline">
            <a href={`mailto:${email}`} className="text-accent-dark underline">
              {email}
            </a>
          </dd>
        </div>
      </dl>
      <a
        href={whatsappUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-3 inline-flex items-center gap-2 rounded-md bg-[#25D366] px-4 py-2.5 text-sm font-semibold text-white hover:brightness-95"
      >
        <WhatsAppIcon />
        Chat with Support on WhatsApp
      </a>
      <div className="mt-4 rounded-md border border-red-300 bg-red-50 px-3 py-2.5 text-red-900">
        <p className="text-sm font-bold">🔒 Security Notice</p>
        <p className="mt-0.5 text-sm">
          Metabridge Academy support will never ask for your Binance Secret Key, password, 2FA code, seed phrase, or
          withdrawal access.
        </p>
      </div>
    </section>
  );
}
