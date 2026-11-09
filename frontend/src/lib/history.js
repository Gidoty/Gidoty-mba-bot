// Filtering and paging for the Trade History table. Pure, so it can be tested.
export const PAGE_SIZE = 25;

// closed: oldest first (lib/stats.js closedTrades). Returns newest first.
export function historyRows(closed, filter = "all") {
  const rows = [...closed].reverse();
  if (filter === "wins") return rows.filter((t) => t.pnl > 0);
  if (filter === "losses") return rows.filter((t) => t.pnl < 0);
  return rows;
}

export function pageOf(rows, pages = 1) {
  return { shown: rows.slice(0, PAGE_SIZE * pages), hasMore: rows.length > PAGE_SIZE * pages };
}

// A trade that closed at exactly 0 is neither a win nor a loss: it only appears under "All".
export function filterCounts(closed) {
  return {
    all: closed.length,
    wins: closed.filter((t) => t.pnl > 0).length,
    losses: closed.filter((t) => t.pnl < 0).length,
  };
}
