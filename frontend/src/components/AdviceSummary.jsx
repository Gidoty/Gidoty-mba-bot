import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "../api/client.js";
import { dashboardNotes } from "../lib/advice.js";
import { navigate } from "../useHashRoute.js";
import { SetupNotes } from "./AdviceNotes.jsx";

// Dashboard: the same quiet button and pop-up as in Bot Settings, with only the notes that need attention (not possible or a warning), at most three. The button sits in the top bar (a slot in
// AppShell: top right on a desktop, in the header on a phone), so it takes no room in the page and nothing moves when it appears. Nothing at all when the advisor is off (404), on any error, or
// when there is nothing to say.
const SLOTS = [
  { id: "advice-slot-desktop", variant: "page" },
  { id: "advice-slot-mobile", variant: "rail" },
];

const focusMain = () => {
  const main = document.querySelector("main");
  main?.setAttribute("tabindex", "-1");
  return main;
};

export default function AdviceSummary({ account }) {
  const [notes, setNotes] = useState([]);
  const [hosts, setHosts] = useState([]);
  useEffect(() => {
    setHosts(SLOTS.map((s) => ({ ...s, el: document.getElementById(s.id) })).filter((s) => s.el));
  }, []);
  useEffect(() => {
    let live = true;
    api.get("/account/advice").then((r) => live && setNotes(dashboardNotes(r.notes)), () => live && setNotes([]));
    return () => {
      live = false;
    };
  }, [account?.risk_pct, account?.allocated_capital, account?.live_slots, account?.daily_loss_limit_usd, account?.max_leverage]);
  if (notes.length === 0) return null;
  const action = { label: "Open Bot Settings", onClick: () => navigate("bot-settings") };
  return hosts.map((h) => createPortal(<SetupNotes key={h.id} notes={notes} variant={h.variant} fallbackFocus={focusMain} action={action} />, h.el));
}
