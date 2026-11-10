import { useMemo, useState } from "react";

import { PnlHero, WinLossCard } from "../components/dashboard/PerformancePanels.jsx";
import useTrades from "../components/dashboard/useTrades.js";
import { Card, CardTitle, EmptyState, ErrorBanner, LoadingBlock, Notice, PageHeader, PnlText, Pill, ResultPill, StatTile } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";
import { GLOSSARY } from "../glossary.js";
import { dayMonthYearTime, signed } from "../lib/format.js";
import { historyRows, pageOf } from "../lib/history.js";
import { closedTrades, isThin, summarize } from "../lib/stats.js";
import { pnlLabels, quoteCurrency } from "../pnlLabels.js";
import { navigate } from "../useHashRoute.js";

// For an account still in practice mode, this page is a short explanation: the Dashboard already
// shows the practice activity. For an account that has gone LIVE, it is a read-only look back at the
// simulated trades from before activation. It has no control that touches Binance credentials,
// live trading or open positions.
export default function Practice() {
  const { account } = useAccount();
  return account.mode !== "live" ? <InPractice /> : <LookBack account={account} />;
}

function InPractice() {
  return (
    <div className="mx-auto grid max-w-3xl gap-4">
      <PageHeader title="Testnet / Practice" subtitle="You're already in practice mode." />
      <Card>
        <CardTitle right={<Pill tone="info">Simulated</Pill>}>How practice mode works</CardTitle>
        <ol className="list-decimal space-y-1.5 pl-5 text-sm text-fg">
          <li>The bot reads prices from Binance's practice server.</li>
          <li>It follows the same strategy and records each practice trade itself.</li>
          <li>No order is sent to Binance and no real money is involved.</li>
          <li>There are no fees and no slippage, so real trades can fill at different prices and cost more.</li>
        </ol>
        <p className="mt-3 text-sm text-muted">Your Dashboard already shows your practice activity.</p>
        <button type="button" onClick={() => navigate("dashboard")} className="mt-3 min-h-[44px] rounded-ctl bg-brand px-5 text-sm font-semibold text-brand-ink hover:brightness-105">
          Go to Dashboard
        </button>
      </Card>
      <WhatIsTestnet />
    </div>
  );
}

function LookBack({ account }) {
  const L = pnlLabels(account, { paper: true });
  const unit = quoteCurrency(account) || "USDT";
  const { status, trades, reload } = useTrades(true, account.id);
  const [pages, setPages] = useState(1);
  const closed = useMemo(() => closedTrades(trades), [trades]);
  const stats = useMemo(() => summarize(closed), [closed]);
  const { shown, hasMore } = pageOf(historyRows(closed), pages);
  const thin = isThin(stats.n);

  return (
    <div className="grid gap-4">
      <PageHeader title="Testnet / Practice" subtitle="A read-only look back at your practice activity before going live.">
        <Pill tone="info">Simulated</Pill>
      </PageHeader>

      <Notice tone="info">
        This is historical only. It reflects trades placed before your account went live, and does not affect your live
        trading, your Binance connection or your open positions in any way. Practice trades are simulated: no order was sent, and there were no fees or slippage.
      </Notice>

      {status === "error" && (
        <div>
          <ErrorBanner message="We could not load your practice trades." />
          <button type="button" onClick={reload} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium hover:bg-surface-2">
            Try again
          </button>
        </div>
      )}
      {status === "loading" && <LoadingBlock />}

      {status === "ready" && (
        <>
          <dl className="grid grid-cols-2 gap-3 shell:grid-cols-4">
            <StatTile label={L.testPnl} value={<PnlText value={stats.totalPnl} unit={unit} />} />
            <StatTile label="Practice trades" value={stats.n} />
            <StatTile
              label={L.winRate}
              value={stats.winRate === null ? "-" : `${stats.winRate.toFixed(0)}%`}
              basis={stats.n && thin ? "not enough trades to read" : null}
              thin={stats.n > 0 && thin}
            />
          </dl>

          {stats.n === 0 ? (
            <Card>
              <EmptyState title="No practice trades on record" body="Practice trades from before you went live would appear here." />
            </Card>
          ) : (
            <div className="grid items-start gap-4 shell:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
              <PnlHero closed={closed} L={L} unit={unit} />
              <WinLossCard stats={stats} />
            </div>
          )}

          {stats.n > 0 && (
            <Card>
              <CardTitle>Practice trade history</CardTitle>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wide text-muted">
                      <th className="px-2 py-2 font-semibold">Closed (UTC)</th>
                      <th className="px-2 py-2 font-semibold">Result</th>
                      <th className="hidden px-2 py-2 font-semibold sm:table-cell">Side</th>
                      <th className="px-2 py-2 text-right font-semibold">{L.tag("P&L")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map((t) => (
                      <tr key={t.id} className="border-t border-line">
                        <td className="px-2 py-2.5 sm:whitespace-nowrap">{dayMonthYearTime(t.ms)}</td>
                        <td className="px-2 py-2.5">
                          <ResultPill pnl={t.pnl} />
                        </td>
                        <td className="hidden px-2 py-2.5 capitalize sm:table-cell">{t.side}</td>
                        <td className={`whitespace-nowrap px-2 py-2.5 text-right font-mono ${t.pnl > 0 ? "text-pos" : t.pnl < 0 ? "text-neg" : "text-muted"}`}>
                          {signed(t.pnl)} {unit}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {hasMore && (
                <div className="mt-3 text-center">
                  <button type="button" onClick={() => setPages((n) => n + 1)} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium hover:bg-surface-2">
                    Show more
                  </button>
                </div>
              )}
            </Card>
          )}
        </>
      )}

      <WhatIsTestnet />
    </div>
  );
}

function WhatIsTestnet() {
  return (
    <details className="rounded-card border border-line bg-surface p-4">
      <summary className="cursor-pointer text-sm font-semibold text-fg">What is Testnet?</summary>
      <p className="mt-2 text-sm text-muted">{GLOSSARY.testnet.long}</p>
    </details>
  );
}
