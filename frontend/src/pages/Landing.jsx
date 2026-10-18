import BrandLogo from "../components/BrandLogo.jsx";
import { navigate } from "../useHashRoute.js";

export default function Landing() {
  return (
    <div className="min-h-screen bg-ink text-white">
      <header className="mx-auto flex max-w-5xl items-center justify-between px-6 py-6">
        <BrandLogo />
        <div className="space-x-3">
          <button onClick={() => navigate("login")} className="text-sm text-slate-300 hover:text-white">
            Log in
          </button>
          <button
            onClick={() => navigate("signup")}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark"
          >
            Create Account
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-2xl px-6 py-20 text-center sm:py-28">
        <h1 className="font-serif text-4xl font-bold leading-tight tracking-tight sm:text-5xl">
          Automated Trading, <span className="text-accent">Built for Simplicity</span>
        </h1>
        <p className="mt-6 text-lg leading-relaxed text-slate-300">
          Metabridge Trading Bot helps you automate supported cryptocurrency trading strategies while giving you
          clear visibility into your trading activity. Start with an unlimited practice environment - no card, no
          risk - and decide for yourself when, if ever, to move to real funds.
        </p>

        <div className="mt-10 flex flex-col items-center gap-3 sm:flex-row sm:justify-center">
          <button
            onClick={() => navigate("signup")}
            className="w-full rounded-md bg-accent px-6 py-3 text-base font-semibold text-white hover:bg-accent-dark sm:w-auto"
          >
            Create Account
          </button>
          <button
            onClick={() => navigate("login")}
            className="w-full rounded-md border border-white/30 px-6 py-3 text-base font-medium text-white hover:bg-white/10 sm:w-auto"
          >
            Log In
          </button>
        </div>

        <dl className="mt-20 grid grid-cols-1 gap-8 text-left sm:grid-cols-3">
          <Feature title="Practice first" body="Every account starts in an unlimited practice environment, free, with no real funds at risk." />
          <Feature title="Risk-managed" body="Every trade sizes itself off your own risk setting and a stop-loss - never a fixed, arbitrary amount." />
          <Feature title="Telegram alerts" body="Entries, exits, and a daily summary sent straight to you." />
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
      <dt className="font-serif text-lg font-semibold text-white">{title}</dt>
      <dd className="mt-1 text-sm text-slate-400">{body}</dd>
    </div>
  );
}
