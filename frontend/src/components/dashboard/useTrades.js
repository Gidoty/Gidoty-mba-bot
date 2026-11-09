import { useCallback, useEffect, useState } from "react";

import { api } from "../../api/client.js";

// Loads GET /trades for one scope (paper or live). status: "loading" | "error" | "ready".
export default function useTrades(isPaper, accountId) {
  const [state, setState] = useState({ status: "loading", trades: [] });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading", trades: [] });
    api
      .get(`/trades?is_paper=${isPaper}`)
      .then((trades) => !cancelled && setState({ status: "ready", trades }))
      .catch(() => !cancelled && setState({ status: "error", trades: [] }));
    return () => {
      cancelled = true;
    };
  }, [isPaper, accountId, tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { ...state, reload };
}
