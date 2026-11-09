import BinanceConnectionPanel from "../components/BinanceConnectionPanel.jsx";
import { linkButton } from "../components/form.jsx";
import { Card, CardTitle, EmptyState, PageHeader } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";
import { navigate } from "../useHashRoute.js";

export default function ExchangeConnection({ customer }) {
  const { account, refreshAccount } = useAccount();

  return (
    <div className="grid gap-4">
      <PageHeader title="Exchange Connection" subtitle="Is Binance connected, and how is it managed." />

      {account.has_mainnet_credential ? (
        <BinanceConnectionPanel account={account} onAccountChanged={refreshAccount} defaultOpen />
      ) : (
        <Card>
          <CardTitle>Binance</CardTitle>
          <EmptyState
            title="No Binance account connected yet"
            body={
              customer.has_paid
                ? "Connect your Binance account from your Dashboard to enable live trading."
                : "Connecting a real Binance account becomes available once your account is activated."
            }
          />
          <button type="button" onClick={() => navigate("dashboard")} className={`mt-3 ${linkButton}`}>
            Go to Dashboard
          </button>
        </Card>
      )}
    </div>
  );
}
