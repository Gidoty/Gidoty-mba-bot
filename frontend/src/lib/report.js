// Wording for the per-account report (GET /api/reports/summary, REPORTING_V2). The server decides the numbers and whether a net figure exists; this only names them.
// Run with: node --test frontend/src/lib/report.test.js (also run by tests/test_frontend_lib.py)
import { fmt, signed } from "./format.js";

// A net figure needs the server's net number, every fee known, and (when the server reports it) every trade's funding known.
function hasNet(group) {
  return !group.simulated && group.fees_complete && group.funding_complete !== false && group.net_pnl !== null && group.net_pnl !== undefined;
}

// What a group's figures are, in words: simulated, net (only when every trade's real fees and funding are known) or before fees.
export function basisLabel(group) {
  if (!group) return "";
  if (group.simulated) return "simulated, no fees or slippage";
  if (hasNet(group)) return "net, after fees and funding";
  if (group.fees_complete && group.funding_complete === false) return "after fees, before funding";
  return "before fees";
}

// The figure to show as the result: net when it exists, otherwise gross. Never a net figure the server did not send.
export function headline(group) {
  if (!group) return null;
  if (hasNet(group)) return group.net_pnl;
  return group.gross_pnl;
}

export function feesText(group) {
  if (!group || group.simulated) return "none (simulated)";
  if (group.fees === null || group.fees === undefined) return "not captured";
  return group.fees_complete ? fmt(group.fees) : `${fmt(group.fees)} (known for ${group.trades_with_fees} of ${group.closed_trades} trades)`;
}

// The funding line (Futures funding payments, signed: negative = paid): its own figure, never folded into gross or fees.
export function fundingText(group) {
  if (!group || group.simulated) return "none (simulated)";
  if (group.funding === null || group.funding === undefined) return "not captured";
  return group.funding_complete === false ? `${signed(group.funding)} (known for ${group.trades_with_funding} of ${group.closed_trades} trades)` : signed(group.funding);
}

// The gross line: before fees and funding.
export function grossText(group) {
  if (!group) return "-";
  return group.gross_pnl === null || group.gross_pnl === undefined ? "-" : signed(group.gross_pnl);
}

// The net line: only the server's number, only when it exists.
export function netText(group, unit) {
  if (!group || !hasNet(group)) return "not known";
  return `${signed(group.net_pnl)} ${unit || ""}`.trim();
}

export function resultText(group, unit) {
  const v = headline(group);
  return v === null || v === undefined ? "-" : `${signed(v)} ${unit || ""}`.trim();
}

export function winRateText(group) {
  const n = group?.closed_trades || 0;
  return n ? `${Math.round(((group.wins || 0) / n) * 100)}%` : "-";
}

export const STRATEGY_NAMES = { ema_cross_atr_v1: "EMA crossover", rsi_mr_v1: "RSI mean reversion", donchian_breakout_v1: "Donchian breakout" };
export function strategyName(id) {
  return STRATEGY_NAMES[id] || id || "EMA crossover";
}
