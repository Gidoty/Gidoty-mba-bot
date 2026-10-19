import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { Card, EmptyState, LoadingBlock, PageHeader, PnlText } from "../components/ui.jsx";
import { OPEN_TRADE_STATUSES } from "../constants.js";
import { useAccount } from "../context/AccountContext.jsx";

export default function Positions() {
  const { account } = useAccount();
  const [trades, setTrades] = useState(undefined);
  const isPaper = account.mode !== "live";

  useEffect(() => {
    api
      .get(`/trades?is_paper=${isPaper}`)
      .then((all) => setTrades(all.filter((t) => OPEN_TRADE_STATUSES.includes(t.status))))
      .catch(() => setTrades([]));
  }, [account.id, isPaper]);

  return (
    <div>
      <PageHeader
        title="Positions"
        subtitle={isPaper ? "Open positions in your practice environment." : "Open positions trading with real funds."}
      />

      {trades === undefined ? (
        <LoadingBlock />
      ) : trades.length === 0 ? (
        <EmptyState title="No open position" body="The bot is monitoring the market for qualifying opportunities." />
      ) : (
        <div className="space-y-2">
          {trades.map((t) => (
            <Card key={t.id}>
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-ink">
                  {t.side.toUpperCase()} {account.pair}
                </p>
                <PnlText value={t.pnl} className="font-semibold" />
              </div>
              <dl className="mt-2 grid grid-cols-3 gap-y-1 text-sm text-slate-600">
                <Field label="Entry" value={t.entry_price} />
                <Field label="Size" value={t.quantity} />
                <Field label="Stop" value={t.stop_loss_price} />
              </dl>
              {!t.is_paper && (
                <p className="mt-1 text-xs text-slate-500">
                  {t.protection_status === "protected"
                    ? "Stop-loss is resting at Binance."
                    : "Stop-loss protection is being set up or checked."}
                </p>
              )}
              <p className="mt-1 text-xs text-slate-400">Opened {new Date(t.opened_at).toLocaleString()}</p>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function Field({ label, value }) {
  return (
    <div>
      <dt className="text-xs uppercase text-slate-400">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}
