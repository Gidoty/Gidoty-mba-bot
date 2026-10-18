import { useState } from "react";

import { api } from "../../api/client.js";
import BinanceConnectionPanel from "../../components/BinanceConnectionPanel.jsx";
import ConsentCheckbox from "../../components/ConsentCheckbox.jsx";
import { Card, ErrorBanner } from "../../components/ui.jsx";

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
    <div>
      <div className="mb-5">
        <h1 className="text-xl font-semibold text-ink">Ready for Live Setup</h1>
        <p className="mt-1 text-sm text-slate-600">
          Your account is activated and your Binance connection is in place. Live trading stays off until you
          explicitly turn it on.
        </p>
      </div>

      <BinanceConnectionPanel account={account} onAccountChanged={onAccountChanged} />

      <ErrorBanner message={error} />

      <Card className="mt-4 border-amber-200 bg-amber-50">
        <p className="text-sm font-semibold text-amber-800">Enable Live Trading</p>
        <p className="mt-1 text-sm text-amber-700">
          Once enabled, the bot will place real orders on Binance using your connected account and your own funds.
        </p>
        <button
          onClick={() => setConfirming(true)}
          className="mt-3 rounded-md bg-red-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-red-700"
        >
          Enable Live Trading
        </button>
      </Card>

      {confirming && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
            <h3 className="text-base font-bold text-red-700">Enable live trading with real funds?</h3>
            <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm font-semibold text-red-800">
              {LIVE_TRADING_RISK_SENTENCE}
            </p>
            <p className="mt-3 text-sm text-slate-600">
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
                    <a
                      href="#/risk-disclosure"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-accent hover:underline"
                    >
                      Trading Risk Disclosure
                    </a>
                  </>
                }
              />
            </div>
            <div className="mt-5 flex flex-wrap justify-end gap-3">
              <button
                onClick={() => {
                  setConfirming(false);
                  setAcceptRiskDisclosure(false);
                }}
                disabled={submitting}
                className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirm}
                disabled={submitting || !acceptRiskDisclosure}
                className="rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-40"
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
