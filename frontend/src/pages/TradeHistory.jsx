import { useMemo, useState } from "react";

import useTrades from "../components/dashboard/useTrades.js";
import { Card, CardTitle, EmptyState, ErrorBanner, LoadingBlock, PageHeader, ResultPill, Seg, ThinNotice, UnprovenNotice } from "../components/ui.jsx";
import { closeReasonLabel } from "../constants.js";
import { useAccount } from "../context/AccountContext.jsx";
import { dayMonthYearTime, signed } from "../lib/format.js";
import { filterCounts, historyRows, pageOf } from "../lib/history.js";
import { fmtPrice, priceDecimals } from "../lib/position.js";
import { closedTrades, isThin, summarize } from "../lib/stats.js";
import { pnlLabels, quoteCurrency } from "../pnlLabels.js";

const tone = (n) => (n > 0 ? "text-pos" : n < 0 ? "text-neg" : "text-muted");
const price = (v) => (v === null || v === undefined ? "-" : fmtPrice(v, priceDecimals(v)));

export default function TradeHistory() {
  const { account } = useAccount();
  const isPaper = account.mode !== "live";
  const L = pnlLabels(account, { paper: isPaper });
  const unit = quoteCurrency(account) || "USDT";
  const { status, trades, reload } = useTrades(isPaper, account.id);
  const [filter, setFilter] = useState("all");
  const [pages, setPages] = useState(1);

  const closed = useMemo(() => closedTrades(trades), [trades]);
  const stats = useMemo(() => summarize(closed), [closed]);
  const counts = useMemo(() => filterCounts(closed), [closed]);
  const rows = useMemo(() => historyRows(closed, filter), [closed, filter]);
  const { shown, hasMore } = pageOf(rows, pages);

  const options = [
    { value: "all", label: `All (${counts.all})` },
    { value: "wins", label: `Wins (${counts.wins})` },
    { value: "losses", label: `Losses (${counts.losses})` },
  ];

  return (
    <div className="grid gap-4">
      <PageHeader title="Trade History" subtitle={isPaper ? "Completed practice trades (simulated)." : "Completed trades using real funds."} />
      <UnprovenNotice paper={isPaper} closedCount={status === "ready" ? stats.n : null} />
      {status === "ready" && stats.n > 0 && isThin(stats.n) && <ThinNotice closedCount={stats.n} />}
      {L.caption && <p className="-mt-2 text-xs text-muted">{L.caption}</p>}

      {status === "error" && (
        <div>
          <ErrorBanner message="We could not load your trades." />
          <button type="button" onClick={reload} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium hover:bg-surface-2">
            Try again
          </button>
        </div>
      )}
      {status === "loading" && <LoadingBlock />}

      {status === "ready" && stats.n === 0 && (
        <Card>
          <EmptyState title="No trades yet" body="Completed trades will appear here once the bot closes a position." />
        </Card>
      )}

      {status === "ready" && stats.n > 0 && (
        <Card>
          <CardTitle right={<Seg label="Filter" value={filter} onChange={(v) => { setFilter(v); setPages(1); }} options={options} />}>
            Closed trades
          </CardTitle>
          <p className="mb-3 text-sm text-muted">
            {rows.length} {rows.length === 1 ? "trade" : "trades"} shown · net{" "}
            <span className={`font-mono ${tone(stats.totalPnl)}`}>
              {signed(stats.totalPnl)} {unit}
            </span>{" "}
            {L.basis ? `${L.basis}, ` : ""}all {stats.n} closed
          </p>

          {rows.length === 0 ? (
            <EmptyState title={filter === "wins" ? "No winning trades yet" : "No losing trades yet"} body="Choose All to see every trade." />
          ) : (
            <>
              <div className="hidden overflow-x-auto sm:block">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wide text-muted">
                      <th className="px-2 py-2 font-semibold">Closed (UTC)</th>
                      <th className="px-2 py-2 font-semibold">Result</th>
                      <th className="px-2 py-2 font-semibold">Side</th>
                      <th className="px-2 py-2 text-right font-semibold">Entry</th>
                      <th className="px-2 py-2 text-right font-semibold">Exit</th>
                      <th className="px-2 py-2 text-right font-semibold">Qty</th>
                      <th className="px-2 py-2 text-right font-semibold">{L.tag("P&L")}</th>
                      <th className="px-2 py-2 font-semibold">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map((t) => (
                      <tr key={t.id} className="border-t border-line hover:bg-surface-2">
                        <td className="whitespace-nowrap px-2 py-2.5">{dayMonthYearTime(t.ms)}</td>
                        <td className="px-2 py-2.5">
                          <ResultPill pnl={t.pnl} />
                        </td>
                        <td className="px-2 py-2.5 capitalize">{t.side}</td>
                        <td className="px-2 py-2.5 text-right font-mono">{price(t.entry_price)}</td>
                        <td className="px-2 py-2.5 text-right font-mono">{price(t.exit_price)}</td>
                        <td className="px-2 py-2.5 text-right font-mono">{t.quantity}</td>
                        <td className={`whitespace-nowrap px-2 py-2.5 text-right font-mono ${tone(t.pnl)}`}>
                          {signed(t.pnl)} {unit}
                        </td>
                        <td className="whitespace-nowrap px-2 py-2.5">{closeReasonLabel(t.close_reason)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <ul className="divide-y divide-line sm:hidden">
                {shown.map((t) => (
                  <li key={t.id} className="py-3">
                    <div className="flex items-center justify-between gap-3">
                      <span className="flex items-center gap-2">
                        <ResultPill pnl={t.pnl} />
                        <span className="text-sm capitalize">{t.side}</span>
                      </span>
                      <span className={`font-mono text-sm ${tone(t.pnl)}`}>
                        {signed(t.pnl)} {unit}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-muted">
                      {dayMonthYearTime(t.ms)} UTC · {closeReasonLabel(t.close_reason)}
                    </p>
                    <p className="mt-0.5 font-mono text-xs text-muted">
                      {price(t.entry_price)} → {price(t.exit_price)} · qty {t.quantity}
                    </p>
                    {L.rowNote && <p className="mt-0.5 text-xs text-muted">{L.rowNote}</p>}
                  </li>
                ))}
              </ul>

              {hasMore && (
                <div className="mt-3 text-center">
                  <button type="button" onClick={() => setPages((n) => n + 1)} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium hover:bg-surface-2">
                    Show more
                  </button>
                </div>
              )}
              <p className="mt-3 text-xs text-muted">Showing {shown.length} of {rows.length}.</p>
            </>
          )}
        </Card>
      )}
    </div>
  );
}
