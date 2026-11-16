// Risk per trade: the bands the screens show and the acknowledgments they ask for (the server enforces the same rules: src/mba_bot/risk_limits.py).
// Up to and including 2%: recommended. Above 2% and below 10%: higher risk. 10% and above: very high risk. The colour is never the only signal: every band has a label and an icon.
export const RECOMMENDED_MAX = 2;
export const VERY_HIGH_FROM = 10;
export const RISK_MIN = 0.1;
const EPS = 1e-9;

export function riskBand(pct) {
  const n = Number(pct);
  if (!Number.isFinite(n)) return "invalid";
  if (n <= RECOMMENDED_MAX + EPS) return "recommended";
  if (n < VERY_HIGH_FROM - EPS) return "high";
  return "very_high";
}

export const BAND_INFO = {
  recommended: { label: "Recommended: 2% or less", icon: "check", tone: "pos" },
  high: { label: "Higher risk", icon: "triangle", tone: "warn" },
  very_high: { label: "Very high risk: a short run of losing trades can remove most of your capital", icon: "octagon", tone: "neg" },
  invalid: { label: "", icon: "", tone: "mute" },
};

export const ACK_TEXT = {
  high: "I understand that a risk per trade above 2% means one losing trade can remove a large part of my trading capital, and I am choosing this setting myself.",
  very_high: "I understand that at 10% or more a short run of losing trades can remove most of my trading capital, and that 2% or less is recommended.",
};

// Which acknowledgments the new value needs: none up to 2%, "high" above 2%, both from 10% up. Lowering back to 2% or below needs none.
export function acknowledgmentsFor(pct) {
  const band = riskBand(pct);
  if (band === "high") return ["high"];
  if (band === "very_high") return ["high", "very_high"];
  return [];
}

// A plain problem text, or null if the value is a number from 0.1 up to the limit.
export function riskProblem(pct, limit) {
  const n = Number(pct);
  if (pct === "" || pct === null || pct === undefined || !Number.isFinite(n)) return "Enter a risk per trade as a number.";
  if (n < RISK_MIN - EPS) return `Risk per trade must be at least ${RISK_MIN}%.`;
  if (n > limit + EPS) return `Risk per trade can be at most ${limit}% on this service.`;
  return null;
}
