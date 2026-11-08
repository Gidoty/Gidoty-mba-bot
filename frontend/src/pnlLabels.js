// Wording for profit figures.
//
// `trades.pnl` is GROSS: (exit - entry) x quantity from the exchange's reported fills, with no exchange fee and no futures
// funding. Practice results are a simulation with no fees and no slippage. The API sets `account.pnl_basis` (and the summary's
// `pnl_basis`) to "gross_before_fees" only while the owner has switched PNL_LABELS on. While it is off, every string below is
// exactly the wording the app always had. No number is changed here, only the words around it.

const FEES_SENTENCE =
  " Exchange fees and futures funding are not included in this figure, so it will not match the change in your Binance balance." +
  " Practice results are simulated and include no fees and no slippage.";

export function quoteCurrency(account) {
  const pair = account && account.pair;
  return pair && pair.includes("/") ? pair.split("/").pop() : "";
}

export function pnlLabels(account, { paper = false } = {}) {
  const on = Boolean(account && account.pnl_basis);
  if (!on) {
    return {
      on: false,
      allTime: "All-Time P&L",
      testPnl: "Test P&L",
      winRate: "Win Rate",
      windowPnl: (label) => `P&L (${label})`,
      cumulative: "Cumulative P&L",
      caption: null,
      rowNote: null,
      unrealized: null,
      unit: "",
      glossary: (long) => long,
    };
  }
  return {
    on: true,
    allTime: paper ? "All-time practice P&L (simulated)" : "All-time P&L (before fees)",
    testPnl: "Practice P&L (simulated, no fees or slippage)",
    winRate: paper ? "Win rate (simulated)" : "Win rate (before fees)",
    windowPnl: (label) => (paper ? `Practice P&L (${label}, simulated)` : `P&L (${label}, before fees)`),
    cumulative: paper ? "Cumulative practice P&L (simulated)" : "Cumulative P&L (before fees)",
    caption: paper
      ? "Practice results are simulated: no fees and no slippage."
      : "Profit figures are before exchange fees and funding. Your Binance balance is the exact figure.",
    rowNote: paper ? "simulated" : "before fees",
    unrealized: paper ? "Unrealized, simulated" : "Unrealized, before fees",
    unit: quoteCurrency(account),
    glossary: (long) => long + FEES_SENTENCE,
  };
}
