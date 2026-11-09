import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client.js";
import PositionCard from "../components/dashboard/PositionCard.jsx";
import useTrades from "../components/dashboard/useTrades.js";
import { Card, CardTitle, EmptyState, ErrorBanner, LoadingBlock, Notice, PageHeader, Pill, UnprovenNotice } from "../components/ui.jsx";
import { OPEN_TRADE_STATUSES } from "../constants.js";
import { useAccount } from "../context/AccountContext.jsx";
import { closedTrades } from "../lib/stats.js";
import { openStatus, tradeToPosition } from "../lib/tradeStatus.js";
import { pnlLabels, quoteCurrency } from "../pnlLabels.js";

export default function Positions() {
  const { account } = useAccount();
  const isPaper = account.mode !== "live";
  const L = pnlLabels(account, { paper: isPaper });
  const unit = quoteCurrency(account) || "USDT";
  const { status, trades, reload } = useTrades(isPaper, account.id);
  const [live, setLive] = useState(undefined); // GET /account/position, live accounts only

  useEffect(() => {
    if (isPaper) {
      setLive(null);
      return;
    }
    api.get("/account/position").then(setLive).catch(() => setLive(null));
  }, [account.id, isPaper]);

  const open = useMemo(() => trades.filter((t) => OPEN_TRADE_STATUSES.includes(t.status)), [trades]);
  const closedCount = useMemo(() => closedTrades(trades).length, [trades]);
  const attention = account.runtime_status === "attention" || open.some((t) => t.status === "attention");

  return (
    <div className="grid gap-4">
      <PageHeader
        title="Positions"
        subtitle={isPaper ? "Open positions in your practice environment (simulated)." : "Open positions trading with real funds."}
      />
      <UnprovenNotice paper={isPaper} closedCount={status === "ready" ? closedCount : null} />

      {status === "error" && (
        <div>
          <ErrorBanner message="We could not load your positions." />
          <button type="button" onClick={reload} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium hover:bg-surface-2">
            Try again
          </button>
        </div>
      )}
      {status === "loading" && <LoadingBlock />}

      {status === "ready" && open.length === 0 && (
        <Card>
          <CardTitle>Open positions</CardTitle>
          <EmptyState title="No open position" body="The bot is monitoring the market for qualifying opportunities." />
        </Card>
      )}

      {status === "ready" && open.length > 0 && (
        <div className="grid items-start gap-4 shell:grid-cols-2">
          {open.map((t) => {
            const st = openStatus(t.status);
            return (
              <PositionCard
                key={t.id}
                title={isPaper ? "Open test position" : "Open position"}
                position={tradeToPosition(t, live)}
                L={L}
                unit={unit}
                badge={<Pill tone={st.tone}>{st.label}</Pill>}
              />
            );
          })}
        </div>
      )}

      <Card>
        <CardTitle>Needs attention</CardTitle>
        {attention ? (
          <Notice>
            A position needs a safety check. The bot is retrying automatically and new trades are on hold until it is resolved. Please also check your Binance account.
          </Notice>
        ) : (
          <p className="flex flex-wrap items-center gap-2 text-sm text-muted">
            <Pill>Nothing right now</Pill>
            {isPaper
              ? "Practice trades are simulated, so there is no stop-loss at Binance to watch."
              : "If Binance does not accept a stop-loss, the bot closes the position and tells you here and on Telegram."}
          </p>
        )}
      </Card>
    </div>
  );
}
