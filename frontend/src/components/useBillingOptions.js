import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client.js";

// GET /api/billing/options. 404 while subscription billing is off, and any error: the answer is "off" and the screens show exactly what they always showed.
export default function useBillingOptions() {
  const [state, setState] = useState({ status: "loading", options: null });
  const load = useCallback(() => {
    let live = true;
    api.get("/billing/options").then(
      (options) => live && setState({ status: "ready", options }),
      () => live && setState({ status: "off", options: null })
    );
    return () => {
      live = false;
    };
  }, []);
  useEffect(() => load(), [load]);
  return state;
}
