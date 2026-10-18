import { useEffect, useState } from "react";

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
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-accent/10 text-2xl text-accent-dark">
          ✓
        </div>
        <h1 className="mt-4 text-xl font-semibold text-ink">Your account is activated</h1>
        <p className="mt-2 max-w-sm text-sm text-slate-600">
          Your Metabridge Trading Bot is now ready for the next stage of setup.
        </p>
        <button
          onClick={onContinue}
          className="mt-6 rounded-md bg-accent px-6 py-3 text-sm font-semibold text-white hover:bg-accent-dark"
        >
          Continue
        </button>
      </div>
    );
  }

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center px-4 text-center">
      <div className="h-10 w-10 animate-spin rounded-full border-4 border-slate-200 border-t-accent" />
      <h1 className="mt-4 text-xl font-semibold text-ink">Verifying your payment</h1>
      <p className="mt-2 max-w-sm text-sm text-slate-600">This usually takes just a few seconds.</p>
      {timedOut && (
        <div className="mt-6">
          <p className="text-sm text-slate-500">Still waiting on confirmation.</p>
          <button
            onClick={manualRefresh}
            disabled={refreshing}
            className="mt-3 rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50 disabled:opacity-60"
          >
            {refreshing ? "Checking..." : "Check again"}
          </button>
        </div>
      )}
    </div>
  );
}
