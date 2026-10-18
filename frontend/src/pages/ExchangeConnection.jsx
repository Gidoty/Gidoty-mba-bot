import BinanceConnectionPanel from "../components/BinanceConnectionPanel.jsx";
import { Card, PageHeader } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";
import { navigate } from "../useHashRoute.js";

export default function ExchangeConnection({ customer }) {
  const { account, refreshAccount } = useAccount();

  return (
    <div>
      <PageHeader title="Exchange Connection" subtitle="Is Binance connected, and how is it managed." />

      {account.has_mainnet_credential ? (
        <BinanceConnectionPanel account={account} onAccountChanged={refreshAccount} />
      ) : (
        <Card>
          <p className="text-sm font-medium text-ink">No Binance account connected yet</p>
          <p className="mt-1 text-sm text-slate-600">
            {customer.has_paid
              ? "Connect your Binance account from your Dashboard to enable live trading."
              : "Connecting a real Binance account becomes available once your account is activated."}
          </p>
          <button onClick={() => navigate("dashboard")} className="mt-3 text-sm font-medium text-accent hover:underline">
            Go to Dashboard
          </button>
        </Card>
      )}
    </div>
  );
}
