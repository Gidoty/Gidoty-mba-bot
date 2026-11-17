import { pairSkipNotice } from "../lib/pairSkip.js";
import { Notice } from "./ui.jsx";

// Shown on the dashboard and in Bot Settings while the bot is skipping the account's pair for being too small for the trading capital.
export default function PairSkipNotice({ account }) {
  const text = pairSkipNotice(account);
  if (!text) return null;
  return <Notice tone="warn">{text}</Notice>;
}
