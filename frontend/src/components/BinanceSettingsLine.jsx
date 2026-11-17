import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client.js";
import { AUTO_SELECT_BINANCE_NOTE, MARGIN_MODE_HELP } from "../leverageHelp.js";
import InfoHelp from "./InfoHelp.jsx";

// "What Binance has for your pair": one read-only line (GET /api/account/binance-settings). The server only ever sends one of a few fixed texts, so this shows exactly what it gets and
// never builds a message from an error. Not applicable for practice and Spot accounts is a plain sentence. The server caches the answer, so "Check again" cannot hammer Binance.
export default function BinanceSettingsLine({ autoSelect = false }) {
  const [state, setState] = useState({ loading: true, text: "", status: "" });

  const load = useCallback(async () => {
    setState((s) => ({ ...s, loading: true }));
    try {
      const r = await api.get("/account/binance-settings");
      setState({ loading: false, text: r.text, status: r.status });
    } catch {
      setState({ loading: false, text: "Could not read your Binance settings for this pair right now. Try again in a minute.", status: "unavailable" });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="mt-3 rounded-ctl border border-line bg-surface-2 px-3 py-2">
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-sm font-medium text-fg">Binance margin mode and leverage</span>
        <InfoHelp label="margin mode" help={MARGIN_MODE_HELP} />
      </div>
      <p role="status" aria-live="polite" className="text-sm text-fg">
        {state.loading ? "Checking Binance..." : state.text}
      </p>
      {autoSelect && state.status !== "not_applicable" && <p className="mt-1 text-sm text-muted">{AUTO_SELECT_BINANCE_NOTE}</p>}
      {!state.loading && state.status !== "not_applicable" && (
        <button type="button" onClick={load} className="mt-1 min-h-[44px] text-sm font-semibold text-brand-text hover:underline">
          Check again
        </button>
      )}
    </div>
  );
}
