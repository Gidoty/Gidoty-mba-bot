import { navigate } from "../useHashRoute.js";

export default function Landing() {
  return (
    <div className="min-h-screen bg-ink text-white">
      <header className="mx-auto flex max-w-5xl items-center justify-between px-6 py-6">
        <span className="text-lg font-semibold">MBA Bot</span>
        <div className="space-x-3">
          <button onClick={() => navigate("login")} className="text-sm text-slate-300 hover:text-white">
            Log in
          </button>
          <button
            onClick={() => navigate("signup")}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium hover:bg-accent-dark"
          >
            Get started
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-24 text-center">
        <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">A trading bot that trades while you watch</h1>
        <p className="mt-6 text-lg text-slate-300">
          EMA crossover strategy with ATR-based risk management on Binance. Every account starts with a 7-day paper
          trial on testnet - no real funds at risk until you say so.
        </p>
        <button
          onClick={() => navigate("signup")}
          className="mt-10 rounded-md bg-accent px-6 py-3 text-base font-medium hover:bg-accent-dark"
        >
          Start your free trial
        </button>

        <dl className="mt-20 grid grid-cols-1 gap-8 text-left sm:grid-cols-3">
          <Feature title="7-day paper trial" body="Watch it trade with simulated funds before risking anything real." />
          <Feature
            title="Risk capped per trade"
            body="1-2% of your balance per trade, sized off an ATR-based stop-loss - never a fixed coin amount."
          />
          <Feature title="Telegram alerts" body="Entries, exits, and a daily P&L summary sent straight to you." />
        </dl>

        <p className="mt-16 text-xs text-slate-500">
          Trading carries real financial risk. This is not financial advice. Past or simulated performance doesn't
          guarantee future results.
        </p>
      </main>
    </div>
  );
}

function Feature({ title, body }) {
  return (
    <div>
      <dt className="font-semibold text-white">{title}</dt>
      <dd className="mt-1 text-sm text-slate-400">{body}</dd>
    </div>
  );
}
