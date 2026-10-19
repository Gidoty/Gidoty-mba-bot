import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { Card, EmptyState, LoadingBlock, PageHeader, PnlText } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";
import { closeReasonLabel } from "../constants.js";

export default function TradeHistory() {
  const { account } = useAccount();
  const [trades, setTrades] = useState(undefined);
  const isPaper = account.mode !== "live";

  useEffect(() => {
    api
      .get(`/trades?is_paper=${isPaper}`)
      .then((all) => setTrades(all.filter((t) => t.status === "closed")))
      .catch(() => setTrades([]));
  }, [account.id, isPaper]);

  return (
    <div>
      <PageHeader
        title="Trade History"
        subtitle={isPaper ? "Completed practice trades." : "Completed trades using real funds."}
      />

      {trades === undefined ? (
        <LoadingBlock />
      ) : trades.length === 0 ? (
        <EmptyState title="No trades yet" body="Completed trades will appear here once the bot closes a position." />
      ) : (
        <div className="space-y-2">
          {trades.map((t) => (
            <Card key={t.id} className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-ink">
                  {t.side.toUpperCase()} {account.pair}
                </p>
                <p className="text-xs text-slate-500">
                  {new Date(t.opened_at).toLocaleDateString()} · {closeReasonLabel(t.close_reason)}
                </p>
              </div>
              <div className="text-right">
                <PnlText value={t.pnl} className="block font-semibold" />
                <p className="text-xs text-slate-400">
                  {t.entry_price} → {t.exit_price}
                </p>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
