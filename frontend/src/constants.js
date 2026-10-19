// Shared with the backend's config.SUPPORTED_PAIRS/SUPPORTED_MARKET_TYPES -
// keep in sync by hand, there's no runtime source of truth shared across
// the two apps.
export const PAIRS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT"];
// Not tradeable yet - CORE isn't listed on Binance. Shown disabled in any
// pair selector; never submitted, so the backend never needs to know
// about it (see UPCOMING_PAIRS in config.py for the same note).
export const UPCOMING_PAIRS = ["CORE/USDT"];

export const MARKET_TYPES = [
  { value: "spot", label: "Spot" },
  { value: "futures", label: "Futures" },
];

// Matches the backend's CREDENTIAL_ENVIRONMENTS (schemas.py) - "unknown"
// is deliberately excluded, it's a migration-only state nobody chooses.
export const CREDENTIAL_ENVIRONMENTS = [
  { value: "testnet", label: "Testnet (practice, no real funds)" },
  { value: "mainnet", label: "Mainnet (real Binance account)" },
];

// Trade statuses that mean "exposure may exist / a trade is in progress",
// matching models.NON_TERMINAL_TRADE_STATUSES on the backend. "void" (an
// entry that never happened) is filtered out by the API and never shown.
export const OPEN_TRADE_STATUSES = ["pending", "protecting", "open", "closing", "attention"];

// Customer-facing wording for Trade.close_reason. Raw reason codes are
// never shown as-is.
export function closeReasonLabel(reason) {
  switch (reason) {
    case "stop_loss":
    case "stop_limit_gap":
      return "Stop-loss";
    case "exchange_stop_triggered":
      return "Stop-loss (at Binance)";
    case "protection_failed_emergency_exit":
      return "Safety exit";
    case "externally_closed":
      return "Closed on Binance";
    case "dust_unprotectable":
      return "Closed";
    default:
      return "Signal exit";
  }
}

// The approved drawer navigation, used on both mobile and desktop (see
// NavDrawer.jsx). No Admin Dashboard entry yet - deliberately left out
// until that's built and real authorization exists for it.
export const NAV_ITEMS = [
  { route: "dashboard", label: "Dashboard" },
  { route: "positions", label: "Positions" },
  { route: "trade-history", label: "Trade History" },
  { route: "performance", label: "Performance" },
  { route: "bot-settings", label: "Bot Settings" },
  { route: "exchange-connection", label: "Exchange Connection" },
  { route: "notifications", label: "Notifications" },
  { route: "practice", label: "Testnet / Practice" },
  { route: "help", label: "Help & Guide" },
  { route: "account", label: "Account" },
];

export const LEGAL_NAV_ITEMS = [
  { route: "terms", label: "Terms of Service" },
  { route: "privacy", label: "Privacy Policy" },
  { route: "risk-disclosure", label: "Trading Risk Disclosure" },
];
