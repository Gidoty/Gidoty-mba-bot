import { useCallback, useEffect, useState } from "react";

import { api } from "../../api/client.js";

// Loads GET /trades for one scope (paper or live). status: "loading" | "error" | "ready".
export default function useTrades(isPaper, accountId, isDemo = false) {
  const [state, setState] = useState({ status: "loading", trades: [] });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading", trades: [] });
    api
      .get(isDemo ? "/trades?is_demo=true" : `/trades?is_paper=${isPaper}`)  // paper, demo and live records are never mixed
      .then((trades) => !cancelled && setState({ status: "ready", trades }))
      .catch(() => !cancelled && setState({ status: "error", trades: [] }));
    return () => {
      cancelled = true;
    };
  }, [isPaper, accountId, isDemo, tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { ...state, reload };
}
