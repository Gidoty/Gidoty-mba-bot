// Number formatting shared by the new screens. Digits use en-US grouping so
// columns line up in the monospace figures; the sign is always explicit for P&L.
export function fmt(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// +12.30, -4.00, 0.00 (a zero has no sign).
export function signed(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  const rounded = Number(n.toFixed(digits));
  if (rounded === 0) return fmt(0, digits);
  return `${rounded > 0 ? "+" : "-"}${fmt(Math.abs(rounded), digits)}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// All dates are shown in UTC, the same clock the bot and the daily summary use.
export function dayMonth(ms) {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

export function dayMonthTime(ms) {
  const d = new Date(ms);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${dayMonth(ms)} ${hh}:${mm}`;
}

export function dayMonthYearTime(ms) {
  return `${dayMonth(ms)} ${new Date(ms).getUTCFullYear()} ${dayMonthTime(ms).split(" ").pop()}`;
}

export function monthName(index) {
  return MONTHS[index];
}
