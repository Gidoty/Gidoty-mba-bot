import { useState } from "react";

const STEPS = [
  {
    title: "1. Create your account",
    body: "Pick a trading pair, choose spot or futures, and paste in a Binance API key with trading enabled and withdrawals switched off. Your funds stay in your own Binance account the whole time - the bot can never withdraw anything.",
  },
  {
    title: "2. Try it free, no risk",
    body: "Every account starts on unlimited paper trading against Binance's testnet - real signals, fake money. There's no time limit and no card required to try it.",
  },
  {
    title: "3. Link Telegram",
    body: "Tap \"Open Telegram and link\" so the bot can message you when it enters or exits a trade, plus a daily summary.",
  },
  {
    title: "4. Unlock live trading when you're ready",
    body: "Tap \"Pay to unlock\" on your dashboard. This is the only thing that turns on real trading with your own funds - paper trading stays free either way, so there's no rush.",
  },
  {
    title: "5. Manage your bot",
    body: "Pause or resume trading anytime from your dashboard. Your bot code at the top is your reference number - have it ready if you ever contact support.",
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
              <h2 className="text-lg font-semibold text-ink">How to use MBA Bot</h2>
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
