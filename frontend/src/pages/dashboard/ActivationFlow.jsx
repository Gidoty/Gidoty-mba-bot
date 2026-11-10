import { useEffect, useState } from "react";

import { primaryButton, secondaryButton } from "../../components/form.jsx";

// Shown while `!customer.has_paid` but the customer has just returned
// from Paystack (DashboardRouter decides that from the URL's query
// string). Polls for a few seconds since the webhook usually beats the
// customer back to the tab; falls back to a manual "Check again" if it
// doesn't. Once `customer.has_paid` flips true, shows the success state
// and waits for a deliberate "Continue" - payment itself never
// auto-advances past this screen into live trading.
export default function ActivationFlow({ customer, onCustomerChanged, onContinue }) {
  const [timedOut, setTimedOut] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (customer.has_paid) return undefined;
    let cancelled = false;
    let attempts = 0;
    const id = setInterval(async () => {
      attempts += 1;
      await onCustomerChanged();
      if (attempts >= 8 && !cancelled) {
        clearInterval(id);
        setTimedOut(true);
      }
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function manualRefresh() {
    setRefreshing(true);
    try {
      await onCustomerChanged();
    } finally {
      setRefreshing(false);
    }
  }

  if (customer.has_paid) {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center px-4 text-center">
        <div aria-hidden="true" className="flex h-16 w-16 items-center justify-center rounded-full bg-pos-soft text-3xl text-pos">
          ✓
        </div>
        <h1 className="mt-5 text-2xl font-bold tracking-tight text-fg">Your account is activated</h1>
        <p className="mt-2 max-w-sm text-sm text-muted">Your Metabridge Trading Bot is now ready for the next stage of setup.</p>
        <ol className="mt-5 max-w-sm list-decimal space-y-1 pl-5 text-left text-sm text-fg">
          <li>Connect your Binance account with a trade-only key.</li>
          <li>Turn on live trading yourself.</li>
          <li>Set your trading capital and start the bot.</li>
        </ol>
        <button type="button" onClick={onContinue} className={`mt-6 ${primaryButton} px-8`}>
          Continue
        </button>
      </div>
    );
  }

  return (
    <div role="status" className="flex min-h-[60vh] flex-col items-center justify-center px-4 text-center">
      <div className="h-10 w-10 animate-spin rounded-full border-4 border-line border-t-brand-line" />
      <h1 className="mt-5 text-2xl font-bold tracking-tight text-fg">Verifying your payment</h1>
      <p className="mt-2 max-w-sm text-sm text-muted">This usually takes just a few seconds.</p>
      {timedOut && (
        <div className="mt-6">
          <p className="text-sm text-muted">Still waiting on confirmation.</p>
          <button type="button" onClick={manualRefresh} disabled={refreshing} className={`mt-3 ${secondaryButton}`}>
            {refreshing ? "Checking..." : "Check again"}
          </button>
        </div>
      )}
    </div>
  );
}
