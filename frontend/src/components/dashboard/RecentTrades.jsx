import { dayMonthTime, signed } from "../../lib/format.js";
import { navigate } from "../../useHashRoute.js";
import { Card, CardTitle, EmptyState, ResultPill } from "../ui.jsx";

// Latest closed trades as a table; P&L carries a sign and a Win or Loss word. `closed` is oldest first.
export default function RecentTrades({ closed, L, unit, title = "Recent trades", emptyBody }) {
  const rows = closed.slice(-5).reverse();
  return (
    <Card>
      <CardTitle
        right={
          <button type="button" onClick={() => navigate("trade-history")} className="text-sm font-semibold text-brand-text hover:underline">
            View all
          </button>
        }
      >
        {title}
      </CardTitle>
      {rows.length === 0 ? (
        <EmptyState title="No closed trades yet" body={emptyBody || "The bot is monitoring the market for qualifying opportunities."} />
      ) : (
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
              {rows.map((t) => (
                <tr key={t.id} className="border-t border-line">
                  <td className="whitespace-nowrap px-2 py-2.5">{dayMonthTime(t.ms)}</td>
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
      )}
    </Card>
  );
}
