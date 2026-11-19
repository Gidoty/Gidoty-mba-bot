import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { dashboardNotes } from "../lib/advice.js";
import { navigate } from "../useHashRoute.js";
import { secondaryButton } from "./form.jsx";
import { Notice } from "./ui.jsx";

// Dashboard: only the setup notes that need attention (not possible or a warning), at most three. Nothing when the advisor is off (404), on any error, or when there is nothing to say.
export default function AdviceSummary({ account }) {
  const [notes, setNotes] = useState([]);
  useEffect(() => {
    let live = true;
    api.get("/account/advice").then((r) => live && setNotes(dashboardNotes(r.notes)), () => live && setNotes([]));
    return () => {
      live = false;
    };
  }, [account?.risk_pct, account?.allocated_capital, account?.live_slots, account?.daily_loss_limit_usd, account?.max_leverage]);
  if (notes.length === 0) return null;
  return (
    <Notice tone="warn">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <ul className="grid gap-1">
          {notes.map((n) => (
            <li key={n.id}>{n.text}</li>
          ))}
        </ul>
        <button type="button" onClick={() => navigate("bot-settings")} className={secondaryButton}>
          Open Bot Settings
        </button>
      </div>
    </Notice>
  );
}
