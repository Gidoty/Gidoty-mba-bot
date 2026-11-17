import { dashboardNotice } from "../lib/lossLimit.js";
import { navigate } from "../useHashRoute.js";
import { Notice } from "./ui.jsx";
import { secondaryButton } from "./form.jsx";

// Shown on the dashboard until the customer has chosen their own daily loss limit (see lib/lossLimit.js dashboardNotice).
export default function DailyLimitNotice({ account }) {
  const n = dashboardNotice(account);
  if (!n) return null;
  return (
    <Notice tone={n.kind === "required" ? "warn" : "info"}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span>{n.text}</span>
        <button type="button" onClick={() => navigate("bot-settings")} className={secondaryButton}>
          Open Bot Settings
        </button>
      </div>
    </Notice>
  );
}
