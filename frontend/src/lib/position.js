// Pure numbers for the open-position card, kept out of the component so they can be tested.
import { fmt } from "./format.js";

// Decimals that keep a price readable at its own scale: 104,250 -> 0, 620.4 -> 2, 0.45 -> 4, 0.0009 -> 6.
export function priceDecimals(price) {
  const p = Math.abs(Number(price));
  if (!Number.isFinite(p) || p === 0) return 2;
  if (p >= 1000) return 0;
  if (p >= 1) return 2;
  if (p >= 0.01) return 4;
  return 6;
}

// One decimals value for every price shown together (Entry, Stop, Now, Stop distance), taken from the
// smallest of them so no label collapses to the same text as its neighbour.
export function sharedDecimals(...prices) {
  const real = prices.filter((p) => p !== null && p !== undefined && Number.isFinite(Number(p)) && Number(p) !== 0);
  if (!real.length) return 2;
  return priceDecimals(Math.min(...real.map((p) => Math.abs(Number(p)))));
}

export function fmtPrice(value, decimals) {
  return fmt(value, decimals);
}

// position: GET /account/position fields. Missing numbers stay null; nothing is invented.
export function positionView(position) {
  const long = String(position.side).toLowerCase() === "long";
  const { entry_price: entry, stop_loss_price: stop, current_price: now, quantity: qty } = position;
  const has = (v) => v !== null && v !== undefined;
  const decimals = sharedDecimals(entry, stop, now);
  const distance = has(stop) && has(now) ? Math.abs(now - stop) : null;
  const distancePct = distance !== null && now ? (distance / Math.abs(now)) * 100 : null;
  // Result if the stop fills at its price: negative is a loss. A long loses when the stop is below entry, a short when it is above.
  const pnlIfStopped = has(stop) && has(entry) && has(qty) ? (long ? stop - entry : entry - stop) * qty : null;
  // Price on the wrong side of the stop: the stop should already have triggered.
  const beyondStop = has(stop) && has(now) ? (long ? now <= stop : now >= stop) : false;
  // Where the stop sits relative to the current price (a stop the price has crossed is on the other side of where it should be).
  const stopSide = has(stop) && has(now) ? (stop < now ? "below" : "above") : null;
  return { long, decimals, distance, distancePct, pnlIfStopped, beyondStop, stopSide, side: long ? "long" : "short" };
}
