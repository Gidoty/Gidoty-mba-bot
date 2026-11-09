import HelpTip from "./HelpTip.jsx";
import { Card, Notice, Pill } from "./ui.jsx";

const STATUS = {
  active: { label: "Active - scanning market", tone: "ok" },
  trade_open: { label: "Position open - monitoring", tone: "info" },
  not_started: { label: "Not started", tone: "mute" },
  paused: { label: "Paused", tone: "mute" },
  attention: { label: "Attention required", tone: "warn" },
  error: { label: "Error", tone: "bad" },
  // trader.py skips an account with no linked Telegram chat entirely.
  needs_telegram: { label: "Waiting for Telegram link - not scanning yet", tone: "warn" },
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
  const status = needsTelegram ? STATUS.needs_telegram : STATUS[account.runtime_status] ?? STATUS.active;
  const lastScanned = formatRelativeTime(account.last_scanned_at);
  const signalLabel = account.last_signal ? SIGNAL_LABELS[account.last_signal] ?? account.last_signal : null;
  const lastSignalTime = formatRelativeTime(account.last_signal_at);

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Pill tone={status.tone}>{status.label}</Pill>
        <HelpTip
          text={
            "Active: the bot is checking the market on schedule - this does not always mean a trade is coming. " +
            "Position open: it's currently holding and monitoring a trade. Not started: live trading is " +
            "authorized but you haven't pressed Start Bot yet. Attention required: your open position needs a " +
            "safety check - the bot is already working on it and new trades are on hold until it is resolved. " +
            "Error: the last check hit a problem - see the message below."
          }
        />
        <p className="text-sm text-muted">
          Last checked: {lastScanned ?? "not yet checked"}
          {" · "}
          Last signal: {signalLabel ? `${signalLabel}${lastSignalTime ? ` (${lastSignalTime})` : ""}` : "not yet evaluated"}
        </p>
      </div>

      <div className="space-y-2 empty:hidden">
        {account.credential_blocked && account.runtime_status !== "error" && (
          <div className="mt-3">
            <Notice>{account.credential_block_message}</Notice>
          </div>
        )}
        {account.runtime_status === "attention" && (
          <div className="mt-3">
            <Notice>
              Your open position needs a safety check. The bot is retrying automatically and new trades are on hold
              until it is resolved. Please also check your Binance account.
            </Notice>
          </div>
        )}
        {account.runtime_status === "error" && account.last_error && (
          <p role="alert" className="mt-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm text-fg">
            {account.last_error}
          </p>
        )}
      </div>
    </Card>
  );
}
