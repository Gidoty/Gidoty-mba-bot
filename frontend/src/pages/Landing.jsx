import BrandLogo from "../components/BrandLogo.jsx";
import { navigate } from "../useHashRoute.js";

// The signed-out front page. It is dark in both themes on purpose (the brand navy), so it does not
// use the theme tokens. Everything stated here is a fact about how the product works; no figure and
// no chart appears, so nothing suggests a result.
const STEPS = [
  ["Create an account", "Pick a trading pair, spot or futures, and your risk per trade. No Binance account is needed yet."],
  ["Practice first", "The bot trades in practice mode with simulated trades. No card, no time limit."],
  ["Link Telegram", "Entries, exits and a daily summary are sent straight to you."],
  ["Go live when you decide", "Activate, connect a trade-only Binance key, and start the bot yourself. Each is a separate step."],
];

const CONTROLS = [
  ["Pair and market", "Choose the pair and spot or USD-M futures."],
  ["Risk per trade", "Set 0.1% to 2% of your balance. Every trade sizes itself from that and a stop-loss."],
  ["Trading capital", "Set a ceiling on how much of your balance the bot may use."],
  ["Your Binance key", "Trade-only, with withdrawals off. Your funds stay in your own Binance account."],
];

export default function Landing() {
  return (
    <div className="min-h-screen bg-rail text-white">
      <header className="mx-auto flex max-w-5xl items-center justify-between px-6 py-6">
        <BrandLogo />
        <div className="flex items-center gap-3">
          <button type="button" onClick={() => navigate("login")} className="min-h-[40px] px-2 text-sm text-slate-300 hover:text-white">
            Log in
          </button>
          <button type="button" onClick={() => navigate("signup")} className="min-h-[40px] rounded-ctl bg-brand px-4 text-sm font-semibold text-brand-ink hover:brightness-105">
            Create Account
          </button>
        </div>
      </header>

      <main>
        <section className="mx-auto max-w-3xl px-6 py-16 text-center sm:py-24">
          <h1 className="font-serif text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            Automated Trading, <span className="text-accent-light">Built for Simplicity</span>
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed text-slate-300">
            Metabridge Trading Bot helps you automate supported cryptocurrency trading strategies while giving you
            clear visibility into your trading activity. Start with an unlimited practice environment - no card, no
            risk - and decide for yourself when, if ever, to move to real funds.
          </p>
          <div className="mt-10 flex flex-col items-center gap-3 sm:flex-row sm:justify-center">
            <button type="button" onClick={() => navigate("signup")} className="min-h-[48px] w-full rounded-ctl bg-brand px-7 text-base font-semibold text-brand-ink hover:brightness-105 sm:w-auto">
              Create Account
            </button>
            <button type="button" onClick={() => navigate("login")} className="min-h-[48px] w-full rounded-ctl border border-white/30 px-7 text-base font-medium text-white hover:bg-white/10 sm:w-auto">
              Log In
            </button>
          </div>
        </section>

        <section className="mx-auto max-w-5xl px-6 pb-16" aria-labelledby="how">
          <h2 id="how" className="text-center text-xs font-semibold uppercase tracking-widest text-rail-text">How it works</h2>
          <ol className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map(([title, body], i) => (
              <li key={title} className="rounded-card border border-white/10 bg-rail-active/60 p-5">
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-brand text-sm font-semibold text-brand-ink" aria-hidden="true">{i + 1}</span>
                <h3 className="mt-3 font-serif text-lg font-semibold">{title}</h3>
                <p className="mt-1 text-sm text-slate-300">{body}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="mx-auto max-w-5xl px-6 pb-20" aria-labelledby="control">
          <h2 id="control" className="text-center text-xs font-semibold uppercase tracking-widest text-rail-text">What you control</h2>
          <dl className="mt-6 grid grid-cols-1 gap-x-10 gap-y-6 sm:grid-cols-2">
            {CONTROLS.map(([title, body]) => (
              <div key={title} className="border-l-2 border-brand-line pl-4">
                <dt className="font-serif text-lg font-semibold">{title}</dt>
                <dd className="mt-1 text-sm text-slate-300">{body}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-16 text-center text-xs text-slate-400">
            Trading carries real financial risk. This is not financial advice. Past or simulated performance doesn't
            guarantee future results.
          </p>
        </section>
      </main>
    </div>
  );
}
