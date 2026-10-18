import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { Card, EmptyState, LoadingBlock, PageHeader, PnlText } from "../components/ui.jsx";
import { GLOSSARY } from "../glossary.js";
import { useAccount } from "../context/AccountContext.jsx";
import { navigate } from "../useHashRoute.js";

// For an account still in Testnet, "Practice" is just the Dashboard -
// no need to duplicate it. For an account that has gone LIVE, this is a
// read-only look back at the practice trades from before activation.
// It intentionally has no controls that touch Mainnet credentials,
// live_trading_enabled_at, or open positions - see ARCHITECTURE.md for
// why a live account can't run a second, ongoing practice sandbox
// alongside its real one without bigger changes this release doesn't make.
export default function Practice() {
  const { account } = useAccount();
  const [trades, setTrades] = useState(undefined);
  const [summary, setSummary] = useState(null);

  useEffect(() => {
    if (account.mode !== "live") return;
    api.get("/trades?is_paper=true").then(setTrades).catch(() => setTrades([]));
    api.get("/trades/summary?is_paper=true").then(setSummary).catch(() => {});
  }, [account.id, account.mode]);

  if (account.mode !== "live") {
    return (
      <div>
        <PageHeader title="Testnet / Practice" subtitle="You're already in the practice environment." />
        <Card>
          <p className="text-sm text-slate-600">
            Your account is currently trading on Testnet - your Dashboard already shows your practice activity.
          </p>
          <button onClick={() => navigate("dashboard")} className="mt-3 text-sm font-medium text-accent hover:underline">
            Go to Dashboard
          </button>
        </Card>
      </div>
    );
  }

  const closed = (trades || []).filter((t) => t.status === "closed");

  return (
    <div>
      <PageHeader title="Testnet / Practice" subtitle="A read-only look back at your practice activity before going live." />

      <Card className="mb-4">
        <p className="text-sm text-slate-600">
          This is historical only - it reflects trades placed before your account went live, and does not affect
          your live trading, Binance connection, or open positions in any way.
        </p>
      </Card>

      {trades === undefined ? (
        <LoadingBlock />
      ) : (
        <>
          {summary && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Card>
                <p className="text-xs uppercase text-slate-500">Practice P&L</p>
                <PnlText value={summary.net_pnl} className="mt-1 block text-lg font-semibold" />
              </Card>
              <Card>
                <p className="text-xs uppercase text-slate-500">Practice Trades</p>
                <p className="mt-1 text-lg font-semibold text-ink">{summary.total_trades}</p>
              </Card>
            </div>
          )}

          <h2 className="mt-6 mb-2 text-sm font-semibold text-slate-700">Practice Trade History</h2>
          {closed.length === 0 ? (
            <EmptyState title="No practice trades on record" />
          ) : (
            <div className="space-y-2">
              {closed.map((t) => (
                <Card key={t.id} className="flex items-center justify-between">
                  <p className="text-sm font-medium text-ink">
                    {t.side.toUpperCase()} {account.pair}
                  </p>
                  <PnlText value={t.pnl} className="font-semibold" />
                </Card>
              ))}
            </div>
          )}
        </>
      )}

      <details className="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <summary className="cursor-pointer text-sm font-semibold text-ink">What is Testnet?</summary>
        <p className="mt-2 text-sm text-slate-600">{GLOSSARY.testnet.long}</p>
      </details>
    </div>
  );
}
