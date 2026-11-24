import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { basisLabel, feesText, fundingText, grossText, netText, resultText, strategyName, winRateText } from "../lib/report.js";
import { Card, CardTitle, EmptyState, ErrorBanner, LoadingBlock, Pill } from "./ui.jsx";

// Results by mode (live, demo, practice kept apart), then by strategy and by pair. The server sends the numbers and whether a net figure exists; "net" is shown only when every trade's real fees
// are known. Shown on the Performance page only when the service offers reports.
function Table({ title, rows, unit, label }) {
  if (!rows.length) return null;
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full min-w-[44rem] text-left text-sm">
        <caption className="sr-only">{title}</caption>
        <thead>
          <tr className="text-xs uppercase tracking-wider text-muted">
            <th scope="col" className="py-1.5 pr-3 font-semibold">{label}</th>
            <th scope="col" className="py-1.5 pr-3 font-semibold">Closed</th>
            <th scope="col" className="py-1.5 pr-3 font-semibold">Win rate</th>
            <th scope="col" className="py-1.5 pr-3 font-semibold">Gross</th>
            <th scope="col" className="py-1.5 pr-3 font-semibold">Fees</th>
            <th scope="col" className="py-1.5 pr-3 font-semibold">Funding</th>
            <th scope="col" className="py-1.5 pr-3 font-semibold">Net</th>
            <th scope="col" className="py-1.5 font-semibold">Basis</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((g) => (
            <tr key={`${g.strategy}-${g.pair}`} className="border-t border-line">
              <th scope="row" className="py-2 pr-3 font-medium text-fg">{g.pair || strategyName(g.strategy)}{g.pair ? <span className="ml-1 text-xs font-normal text-muted">{strategyName(g.strategy)}</span> : null}</th>
              <td className="py-2 pr-3">{g.closed_trades}</td>
              <td className="py-2 pr-3">{winRateText(g)}</td>
              <td className="py-2 pr-3">{grossText(g)}</td>
              <td className="py-2 pr-3 text-muted">{feesText(g)}</td>
              <td className="py-2 pr-3 text-muted">{fundingText(g)}</td>
              <td className="py-2 pr-3 font-medium">{netText(g, unit)}</td>
              <td className="py-2 text-muted">{basisLabel(g)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ReportPanel({ account }) {
  const [state, setState] = useState({ status: "loading", report: null });
  useEffect(() => {
    if (!account.reports_available) return undefined;
    let cancelled = false;
    api
      .get("/reports/summary")
      .then((report) => !cancelled && setState({ status: "ready", report }))
      .catch(() => !cancelled && setState({ status: "error", report: null }));
    return () => {
      cancelled = true;
    };
  }, [account.id, account.reports_available]);
  if (!account.reports_available) return null;
  if (state.status === "loading") return <LoadingBlock />;
  if (state.status === "error") return <ErrorBanner message="We could not load your report." />;
  const { report } = state;
  const unit = report.quote_currency || "USDT";
  return (
    <Card>
      <CardTitle>Results by strategy and pair</CardTitle>
      <p className="text-sm text-muted">Live, demo and practice results are never added together. A figure is called net only when the real fees and the Futures funding of every trade in it are known; otherwise it is before fees or before funding. {report.funding_note}</p>
      {report.modes.length === 0 && <EmptyState title="No closed trades yet" body="Your report appears after the first closed trade." />}
      {report.modes.map((m) => (
        <section key={m.mode} className="mt-4" aria-label={m.label}>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-fg">{m.label}</h3>
            <Pill tone={m.mode === "live" ? "ok" : m.mode === "demo" ? "warn" : "mute"}>{resultText(m.total, unit)}, {basisLabel(m.total)}</Pill>
          </div>
          <Table title={`${m.label}: by strategy`} rows={m.by_strategy} unit={unit} label="Strategy" />
          <Table title={`${m.label}: by pair`} rows={m.by_pair} unit={unit} label="Pair" />
        </section>
      ))}
    </Card>
  );
}
