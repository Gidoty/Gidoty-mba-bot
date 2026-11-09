import { useState } from "react";

import { api } from "../api/client.js";
import ConsentCheckbox from "./ConsentCheckbox.jsx";

// Shown instead of Onboarding/Dashboard whenever the backend's
// require_legal_acceptance dependency 403s /api/account/* with
// ACCEPTANCE_REQUIRED (see App.jsx) - most notably for every customer
// who signed up before these documents existed and therefore has zero
// acceptance rows on file. Nothing lets them past this except actually
// checking both boxes; there is no backdated/implicit acceptance.
export default function LegalConsentGate({ onAccepted, onLogout }) {
  const [acceptTerms, setAcceptTerms] = useState(false);
  const [acknowledgePrivacy, setAcknowledgePrivacy] = useState(false);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.post("/auth/accept-legal", {
        accept_terms: acceptTerms,
        acknowledge_privacy: acknowledgePrivacy,
      });
      await onAccepted();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-app px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md space-y-4 rounded-lg border border-line bg-surface p-8 shadow-sm"
      >
        <h1 className="text-xl font-semibold text-fg">Updated Terms of Service and Privacy Policy</h1>
        <p className="text-sm text-muted">
          We've introduced a Terms of Service and a Privacy Policy. Please review and accept them to continue
          using your account.
        </p>

        {error && <p className="rounded-md bg-neg-soft px-3 py-2 text-sm text-fg">{error}</p>}

        <ConsentCheckbox
          checked={acceptTerms}
          onChange={setAcceptTerms}
          label={
            <>
              I accept the{" "}
              <a href="#/terms" target="_blank" rel="noopener noreferrer" className="text-brand-text hover:underline">
                Terms of Service
              </a>
            </>
          }
        />
        <ConsentCheckbox
          checked={acknowledgePrivacy}
          onChange={setAcknowledgePrivacy}
          label={
            <>
              I acknowledge the{" "}
              <a href="#/privacy" target="_blank" rel="noopener noreferrer" className="text-brand-text hover:underline">
                Privacy Policy
              </a>
            </>
          }
        />

        <button
          type="submit"
          disabled={!acceptTerms || !acknowledgePrivacy || submitting}
          className="w-full rounded-md bg-brand px-4 py-2 font-medium text-brand-ink hover:brightness-105 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting ? "Saving..." : "Continue"}
        </button>
        <button
          type="button"
          onClick={onLogout}
          className="w-full text-center text-sm text-muted hover:text-fg"
        >
          Log out instead
        </button>
      </form>
    </div>
  );
}
