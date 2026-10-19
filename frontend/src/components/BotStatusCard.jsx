import HelpTip from "./HelpTip.jsx";

const STATUS_STYLES = {
  active: { label: "Active - scanning market", dot: "bg-accent", text: "text-accent-dark", bg: "bg-accent/5 border-accent/30" },
  trade_open: { label: "Position open - monitoring", dot: "bg-sky-500", text: "text-sky-800", bg: "bg-sky-50 border-sky-200" },
  not_started: { label: "Not started", dot: "bg-slate-400", text: "text-slate-600", bg: "bg-slate-50 border-slate-200" },
  paused: { label: "Paused", dot: "bg-slate-400", text: "text-slate-600", bg: "bg-slate-50 border-slate-200" },
  attention: { label: "Attention required", dot: "bg-amber-500", text: "text-amber-900", bg: "bg-amber-50 border-amber-300" },
  error: { label: "Error", dot: "bg-red-500", text: "text-red-800", bg: "bg-red-50 border-red-200" },
  // trader.py skips an account with no linked Telegram chat entirely.
  needs_telegram: { label: "Waiting for Telegram link - not scanning yet", dot: "bg-amber-500", text: "text-amber-900", bg: "bg-amber-50 border-amber-300" },
};

const SIGNAL_LABELS = {
  none: "No qualifying signal yet",
  bullish_crossover: "Bullish crossover (buy signal)",
  bearish_crossover: "Bearish crossover (sell signal)",
};

function formatRelativeTime(iso) {
  if (!iso) return null;
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

// Shared between the Testnet and Live dashboards - the same runtime
// fields mean the same thing in either mode, just against different
// money.
export default function BotStatusCard({ account }) {
  const needsTelegram = account.notification_channel === "telegram" && !account.notification_target;
  const style = needsTelegram ? STATUS_STYLES.needs_telegram : STATUS_STYLES[account.runtime_status] ?? STATUS_STYLES.active;
  const lastScanned = formatRelativeTime(account.last_scanned_at);
  const signalLabel = account.last_signal ? SIGNAL_LABELS[account.last_signal] ?? account.last_signal : null;
  const lastSignalTime = formatRelativeTime(account.last_signal_at);

  return (
    <div className={`rounded-lg border p-4 ${style.bg}`}>
      <div className="flex items-center gap-2">
        <span className={`h-2.5 w-2.5 rounded-full ${style.dot}`} />
        <span className={`text-sm font-semibold ${style.text}`}>{style.label}</span>
        <HelpTip
          text={
            "Active: the bot is checking the market on schedule - this does not always mean a trade is coming. " +
            "Position open: it's currently holding and monitoring a trade. Not started: live trading is " +
            "authorized but you haven't pressed Start Bot yet. Attention required: your open position needs a " +
            "safety check - the bot is already working on it and new trades are on hold until it is resolved. " +
            "Error: the last check hit a problem - see the message below."
          }
        />
      </div>

      <p className="mt-2 text-xs text-slate-500">
        Last checked: {lastScanned ?? "not yet checked"}
        {" · "}
        Last signal: {signalLabel ? `${signalLabel}${lastSignalTime ? ` (${lastSignalTime})` : ""}` : "not yet evaluated"}
      </p>

      {account.credential_blocked && account.runtime_status !== "error" && (
        <p className="mt-2 rounded-md bg-amber-100 px-3 py-2 text-xs text-amber-900">{account.credential_block_message}</p>
      )}

      {account.runtime_status === "attention" && (
        <p className="mt-2 rounded-md bg-amber-100 px-3 py-2 text-xs text-amber-900">
          Your open position needs a safety check. The bot is retrying automatically and new trades are on hold
          until it is resolved. Please also check your Binance account.
        </p>
      )}

      {account.runtime_status === "error" && account.last_error && (
        <p className="mt-2 rounded-md bg-red-100 px-3 py-2 text-xs text-red-800">{account.last_error}</p>
      )}
    </div>
  );
}
