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
