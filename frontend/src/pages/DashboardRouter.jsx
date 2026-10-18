import { useState } from "react";

import { api } from "../api/client.js";
import { useAccount } from "../context/AccountContext.jsx";
import ActivationFlow from "./dashboard/ActivationFlow.jsx";
import ConnectBinance from "./dashboard/ConnectBinance.jsx";
import LiveDashboard from "./dashboard/LiveDashboard.jsx";
import ReadyForLive from "./dashboard/ReadyForLive.jsx";
import TestnetDashboard from "./dashboard/TestnetDashboard.jsx";

function hasPaystackReturnParams() {
  const params = new URLSearchParams(window.location.search);
  return params.has("reference") || params.has("trxref");
}

function clearPaystackReturnParams() {
  const url = new URL(window.location.href);
  url.searchParams.delete("reference");
  url.searchParams.delete("trxref");
  window.history.replaceState(null, "", url.pathname + url.search + url.hash);
}

// The single state machine that decides what "Dashboard" actually shows:
// Testnet (unpaid) -> Activation (just returned from Paystack) ->
// Connect Binance (paid, no mainnet credential) -> Ready for Live (paid,
// mainnet credential, not yet enabled) -> Live Dashboard. Every branch
// here reads fields the backend already computes (has_paid,
// has_mainnet_credential, live_trading_enabled_at, mode) - nothing here
// decides trading permissions, it only decides which screen to show.
export default function DashboardRouter({ customer, onCustomerChanged }) {
  const { account, refreshAccount } = useAccount();
  const [activating, setActivating] = useState(hasPaystackReturnParams());

  async function startActivation() {
    try {
      const { authorization_url } = await api.post("/billing/initialize");
      window.location.href = authorization_url;
    } catch (err) {
      alert(err.message); // eslint-disable-line no-alert
    }
  }

  if (activating) {
    return (
      <ActivationFlow
        customer={customer}
        onCustomerChanged={onCustomerChanged}
        onContinue={() => {
          clearPaystackReturnParams();
          setActivating(false);
        }}
      />
    );
  }

  if (!customer.has_paid) {
    return <TestnetDashboard account={account} onAccountChanged={refreshAccount} onActivate={startActivation} />;
  }

  if (account.mode === "live") {
    return <LiveDashboard account={account} />;
  }

  if (!account.has_mainnet_credential) {
    return <ConnectBinance onAccountChanged={refreshAccount} />;
  }

  return <ReadyForLive account={account} onAccountChanged={refreshAccount} />;
}
