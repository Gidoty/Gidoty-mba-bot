import { useState } from "react";

const STEPS = [
  {
    title: "1. Create your account",
    body: "Pick a trading pair, choose spot or futures, and set your risk level. No Binance account is needed yet.",
  },
  {
    title: "2. Try it free, no risk",
    body: "Every account starts in an unlimited practice environment against Binance's testnet - real signals, no real money. There's no time limit and no card required to try it.",
  },
  {
    title: "3. Link Telegram",
    body: "Tap \"Open Telegram and link\" so the bot can message you when it enters or exits a trade, plus a daily summary.",
  },
  {
    title: "4. Activate when you're ready",
    body: "Tap \"Activate\" on your dashboard to unlock the full trading environment through a one-time payment. This does not turn on real trading by itself.",
  },
  {
    title: "5. Connect Binance and go live",
    body: "Once activated, connect a Binance account with a trade-only key, then explicitly enable live trading - two separate, deliberate steps, each with its own confirmation.",
  },
  {
    title: "6. Manage your bot",
    body: "Pause or resume trading anytime from Bot Settings. Your bot code in the menu is your reference number - have it ready if you ever contact support.",
  },
];

export default function HowToUseGuide() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed bottom-4 right-4 z-40 flex items-center gap-1.5 rounded-full bg-ink px-4 py-2 text-xs font-semibold text-white shadow-lg hover:bg-ink/90"
      >
        <span className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-white/70 text-[10px] leading-none">
          ?
        </span>
        How to use this bot
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-4 sm:items-center"
          onClick={() => setOpen(false)}
        >
          <div
            className="max-h-[80vh] w-full max-w-md overflow-y-auto rounded-lg bg-white p-6 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-ink">How to use Metabridge Trading Bot</h2>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="text-slate-400 hover:text-ink"
              >
                ✕
              </button>
            </div>
            <ol className="space-y-4 text-sm text-slate-600">
              {STEPS.map((step) => (
                <li key={step.title}>
                  <p className="font-medium text-ink">{step.title}</p>
                  <p className="mt-0.5">{step.body}</p>
                </li>
              ))}
            </ol>
          </div>
        </div>
      )}
    </>
  );
}
