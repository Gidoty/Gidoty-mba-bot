// Customer wording for a trade that is not closed yet (Trade.status), and how a trade row becomes
// the position the card draws. Pure, so it can be tested.
const STATUS = {
  pending: { label: "Placing the entry", tone: "info" },
  protecting: { label: "Setting up the stop-loss", tone: "warn" },
  open: { label: "Open", tone: "ok" },
  closing: { label: "Closing", tone: "info" },
  attention: { label: "Needs attention", tone: "warn" },
};

export function openStatus(status) {
  return STATUS[status] || { label: "Open", tone: "mute" };
}

// `live` is the answer of GET /account/position (or null). It carries the current price and the
// unrealised result for the one open live trade; practice trades have neither.
export function tradeToPosition(trade, live = null) {
  const merge = live && live.has_position && !trade.is_paper;
  return {
    side: trade.side,
    entry_price: trade.entry_price,
    stop_loss_price: trade.stop_loss_price,
    quantity: trade.quantity,
    opened_at: trade.opened_at,
    current_price: merge ? live.current_price ?? null : null,
    unrealized_pnl: merge ? live.unrealized_pnl ?? null : null,
    protected: trade.is_paper ? null : merge && live.protected !== undefined ? live.protected : trade.protection_status === "protected",
  };
}
