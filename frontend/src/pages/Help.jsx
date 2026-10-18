import { PageHeader } from "../components/ui.jsx";
import { GLOSSARY } from "../glossary.js";

const TOPICS = [
  { title: "What is Testnet?", body: GLOSSARY.testnet.long },
  {
    title: "How does automated trading work?",
    body: "The bot checks the market on a schedule and follows a fixed strategy - it does not guess, and it does not need your confirmation before every trade. You choose your pair, market, and risk level; the bot handles entries and exits within those settings.",
  },
  { title: "What is Spot trading?", body: GLOSSARY.spot.long },
  { title: "What is Futures trading?", body: GLOSSARY.futures.long },
  { title: "What is a trading pair?", body: GLOSSARY.pair.long },
  { title: "What is P&L?", body: GLOSSARY.pnl.long },
  { title: "What does Bot Active mean?", body: GLOSSARY.botActive.long },
  { title: "What does Scanning mean?", body: GLOSSARY.scanning.long },
  { title: "What is an open position?", body: GLOSSARY.position.long },
  {
    title: "Understanding trade history",
    body: "Trade History shows every completed trade, including how it was closed - either because it hit its stop-loss, or because the strategy's signal reversed. It never shows a trade that's still open; those appear under Positions.",
  },
  {
    title: "How activation works",
    body: "Activating unlocks the full trading environment through a one-time payment. It does not turn on live trading by itself - you still connect a Binance account and explicitly enable live trading as separate steps afterward.",
  },
  {
    title: "Connecting Binance",
    body: "Once activated, you can connect a Binance account with a trade-only API key (withdrawals switched off). This becomes available only after activation - it's never requested before then.",
  },
  {
    title: "Managing your Binance connection",
    body: "From Exchange Connection, you can check your connection status, test it, or replace your stored credentials. There's no way to fully disconnect yet - replacing your key with a testnet one is the way to step back from live trading.",
  },
  {
    title: "Telegram notifications",
    body: "Linking Telegram lets the bot message you when it opens or closes a trade, plus a daily summary. The bot won't trade at all until this is linked.",
  },
  {
    title: "Moving between Testnet and Live",
    body: "Testnet and Live are kept strictly separate. Going live never deletes or changes your practice history, and visiting Testnet/Practice after going live never touches your live credentials, trading mode, or open positions.",
  },
  {
    title: "Common status messages",
    body: "Active: the bot is checking the market on schedule. Position open: it's holding a trade. Paused: you turned trading off. Needs attention: the last check hit a problem, shown with a plain-language explanation.",
  },
];

export default function Help() {
  return (
    <div>
      <PageHeader title="Help & Guide" subtitle="Plain explanations for anyone new to automated trading." />
      <div className="space-y-2">
        {TOPICS.map((topic) => (
          <details key={topic.title} className="rounded-lg border border-slate-200 bg-white p-4">
            <summary className="cursor-pointer text-sm font-medium text-ink">{topic.title}</summary>
            <p className="mt-2 text-sm text-slate-600">{topic.body}</p>
          </details>
        ))}
      </div>
    </div>
  );
}
