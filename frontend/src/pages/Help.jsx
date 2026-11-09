import { useMemo, useState } from "react";

import { FormField, inputClass } from "../components/form.jsx";
import { Card, CardTitle, EmptyState, PageHeader } from "../components/ui.jsx";
import { BINANCE_GUIDE_SUPPORT } from "../constants.js";
import { useAccount } from "../context/AccountContext.jsx";
import { GLOSSARY } from "../glossary.js";
import { pnlLabels } from "../pnlLabels.js";

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
    body: "Active: the bot is checking the market on schedule. Position open: it's holding and monitoring a trade. Not started: live trading is authorized but you haven't pressed Start Bot yet. Needs attention: the last check hit a problem, shown with a plain-language explanation.",
  },
];

// Which heading each topic sits under. A topic missing here falls under "More".
const GROUPS = {
  "What is Testnet?": "Getting started",
  "How does automated trading work?": "Getting started",
  "How activation works": "Getting started",
  "Connecting Binance": "Getting started",
  "Telegram notifications": "Getting started",
  "What is Spot trading?": "Trading basics",
  "What is Futures trading?": "Trading basics",
  "What is a trading pair?": "Trading basics",
  "What is P&L?": "Trading basics",
  "What does Bot Active mean?": "Using the bot",
  "What does Scanning mean?": "Using the bot",
  "What is an open position?": "Using the bot",
  "Understanding trade history": "Using the bot",
  "Common status messages": "Using the bot",
  "Managing your Binance connection": "Your account",
  "Moving between Testnet and Live": "Your account"
};
const GROUP_ORDER = ["Getting started", "Trading basics", "Using the bot", "Your account", "More"];

export default function Help() {
  const { account } = useAccount();
  const L = pnlLabels(account, { paper: account ? account.mode !== "live" : false });
  const [query, setQuery] = useState("");

  const sections = useMemo(() => {
    const q = query.trim().toLowerCase();
    const text = (t) => (t.title === "What is P&L?" ? L.glossary(t.body) : t.body);
    const hits = TOPICS.filter((t) => !q || t.title.toLowerCase().includes(q) || text(t).toLowerCase().includes(q));
    return GROUP_ORDER.map((name) => ({ name, items: hits.filter((t) => (GROUPS[t.title] || "More") === name).map((t) => ({ ...t, text: text(t) })) })).filter((g) => g.items.length);
  }, [query, L]);

  return (
    <div className="grid gap-4">
      <PageHeader title="Help & Guide" subtitle="Plain explanations for anyone new to automated trading." />

      <FormField label="Search the help topics">
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="For example: stop-loss, Telegram, futures" className={inputClass} />
      </FormField>

      {sections.length === 0 && (
        <Card>
          <EmptyState title="No topic matches that search" body="Try a shorter word, or clear the search to see every topic." />
        </Card>
      )}

      {sections.map((section) => (
        <Card key={section.name}>
          <CardTitle>{section.name}</CardTitle>
          <div className="divide-y divide-line">
            {section.items.map((topic) => (
              <details key={topic.title} className="group py-3" open={Boolean(query.trim())}>
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium text-fg">
                  {topic.title}
                  <span aria-hidden="true" className="text-muted transition-transform group-open:rotate-90">›</span>
                </summary>
                <p className="mt-2 text-sm text-muted">{topic.text}</p>
              </details>
            ))}
          </div>
        </Card>
      ))}

      <Card>
        <CardTitle>Still stuck?</CardTitle>
        <p className="text-sm text-muted">
          Have your bot code ready (it is shown under the logo) and contact support on WhatsApp at {BINANCE_GUIDE_SUPPORT.whatsappDisplay} or by email.
        </p>
        <p className="mt-2 text-sm">
          <a className="font-semibold text-brand-text hover:underline" href={`mailto:${BINANCE_GUIDE_SUPPORT.email}`}>
            {BINANCE_GUIDE_SUPPORT.email}
          </a>
        </p>
      </Card>
    </div>
  );
}
