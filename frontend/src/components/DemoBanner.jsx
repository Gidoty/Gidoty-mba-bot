import { Notice } from "./ui.jsx";

// Shown on every page while the account is in DEMO mode, so a demo screen can never be mistaken for a live one.
export default function DemoBanner({ account }) {
  if (account?.mode !== "demo") return null;
  return (
    <Notice tone="info">
      <strong className="font-semibold">DEMO.</strong> Practice orders on Binance&apos;s demo system. No real money is used and nothing here is a live result.
    </Notice>
  );
}
