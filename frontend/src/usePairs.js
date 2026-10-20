import { useEffect, useState } from "react";

import { api } from "./api/client.js";
import { PAIRS } from "./constants.js";

// Exactly today's list. It is what the dropdowns show on first render and
// whenever the backend cannot answer (older backend without /pairs, network
// error): the frontend therefore works unchanged regardless of deploy order.
const FALLBACK = PAIRS.map((pair) => ({ pair, available: true, reason: null }));

// The pairs selectable for `marketType`, each marked available or unavailable
// with a customer-facing reason. `refreshKey` (e.g. the saved trading capital)
// re-reads the list when something it depends on changes.
export function usePairs(marketType, refreshKey) {
  const [state, setState] = useState({ pairs: FALLBACK, loaded: false });

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/pairs?market_type=${encodeURIComponent(marketType)}`)
      .then((data) => {
        if (!cancelled && Array.isArray(data?.pairs) && data.pairs.length > 0) {
          setState({ pairs: data.pairs, loaded: true });
        }
      })
      .catch(() => {
        if (!cancelled) setState({ pairs: FALLBACK, loaded: false });
      });
    return () => {
      cancelled = true;
    };
  }, [marketType, refreshKey]);

  return state;
}

// The option list for a <select>: the server's list, plus the currently
// selected pair if it is not in it (so a saved selection is never silently
// swapped for another one in the display).
export function withCurrentPair(pairs, current) {
  if (!current || pairs.some((p) => p.pair === current)) return pairs;
  return [...pairs, { pair: current, available: true, reason: null }];
}

export function optionLabel(p) {
  return p.available ? p.pair : `${p.pair} - ${p.reason || "Unavailable"}`;
}
