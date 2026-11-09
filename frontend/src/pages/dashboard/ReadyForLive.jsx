import { useState } from "react";

import { api } from "../../api/client.js";
import BinanceConnectionPanel from "../../components/BinanceConnectionPanel.jsx";
import ConsentCheckbox from "../../components/ConsentCheckbox.jsx";
import { secondaryButton } from "../../components/form.jsx";
import { Card, CardTitle, ErrorBanner, PageHeader, Pill } from "../../components/ui.jsx";

const LIVE_TRADING_RISK_SENTENCE =
  "You are enabling LIVE automated cryptocurrency trading. Real orders may be submitted to your connected " +
  "Binance account using real funds. Trading can result in substantial financial loss.";

export default function ReadyForLive({ account, onAccountChanged }) {
  const [confirming, setConfirming] = useState(false);
  const [acceptRiskDisclosure, setAcceptRiskDisclosure] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  async function handleConfirm() {
    setSubmitting(true);
    setError(null);
    try {
      await api.post("/account/enable-live-trading", { accept_risk_disclosure: acceptRiskDisclosure });
      setConfirming(false);
      onAccountChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="grid gap-4">
      <PageHeader
        title="Ready for Live Setup"
        subtitle="Your account is activated and your Binance connection is in place. Live trading stays off until you explicitly turn it on."
      />

      <BinanceConnectionPanel account={account} onAccountChanged={onAccountChanged} />

      <ErrorBanner message={error} />

      <Card className="border-l-4 border-l-warn">
        <CardTitle right={<Pill tone="warn">Off</Pill>}>Enable live trading</CardTitle>
        <p className="text-sm text-muted">
          Once enabled, the bot will place real orders on Binance using your connected account and your own funds.
        </p>
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="mt-4 inline-flex min-h-[44px] items-center justify-center rounded-ctl bg-red-600 px-5 text-sm font-semibold text-white hover:bg-red-700"
        >
          Enable Live Trading
        </button>
      </Card>

      {confirming && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div role="dialog" aria-modal="true" aria-label="Enable live trading" className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-card border border-line bg-surface p-6 shadow-xl">
            <h3 className="text-base font-bold text-neg">Enable live trading with real funds?</h3>
            <p className="mt-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm font-semibold text-fg">
              {LIVE_TRADING_RISK_SENTENCE}
            </p>
            <p className="mt-3 text-sm text-muted">
              The bot's signals are not guaranteed to be profitable, and you are solely responsible for any losses
              incurred.
            </p>
            <div className="mt-4">
              <ConsentCheckbox
                checked={acceptRiskDisclosure}
                onChange={setAcceptRiskDisclosure}
                label={
                  <>
                    I acknowledge the{" "}
                    <a href="#/risk-disclosure" target="_blank" rel="noopener noreferrer" className="font-medium text-brand-text hover:underline">
                      Trading Risk Disclosure
                    </a>
                  </>
                }
              />
            </div>
            <div className="mt-5 flex flex-wrap justify-end gap-3">
              <button
                type="button"
                onClick={() => {
                  setConfirming(false);
                  setAcceptRiskDisclosure(false);
                }}
                disabled={submitting}
                className={secondaryButton}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirm}
                disabled={submitting || !acceptRiskDisclosure}
                className="inline-flex min-h-[44px] items-center justify-center rounded-ctl bg-red-600 px-4 text-sm font-semibold text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {submitting ? "Enabling..." : "Yes, enable live trading with real funds"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
