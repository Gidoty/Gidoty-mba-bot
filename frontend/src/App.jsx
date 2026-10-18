import { useEffect, useState } from "react";

import { ApiError, api } from "./api/client.js";
import Footer from "./components/Footer.jsx";
import HowToUseGuide from "./components/HowToUseGuide.jsx";
import LegalConsentGate from "./components/LegalConsentGate.jsx";
import { PRIVACY_POLICY, RISK_DISCLOSURE, TERMS_OF_SERVICE } from "./legalContent.js";
import Dashboard from "./pages/Dashboard.jsx";
import Landing from "./pages/Landing.jsx";
import LegalPage from "./pages/LegalPage.jsx";
import Login from "./pages/Login.jsx";
import Onboarding from "./pages/Onboarding.jsx";
import Signup from "./pages/Signup.jsx";
import { navigate, useHashRoute } from "./useHashRoute.js";

// Static legal pages - reachable regardless of auth state, and
// deliberately checked before anything that depends on login/consent.
const LEGAL_ROUTES = {
  terms: TERMS_OF_SERVICE,
  "risk-disclosure": RISK_DISCLOSURE,
  privacy: PRIVACY_POLICY,
};

export default function App() {
  const route = useHashRoute();
  const [customer, setCustomer] = useState(undefined); // undefined = loading, null = logged out

  function refreshCustomer() {
    return api
      .get("/auth/me")
      .then(setCustomer)
      .catch(() => setCustomer(null));
  }

  useEffect(refreshCustomer, []);

  function handleAuthed(nextCustomer) {
    setCustomer(nextCustomer);
    navigate("app");
  }

  function handleLogout() {
    api.post("/auth/logout").finally(() => {
      setCustomer(null);
      navigate("landing");
    });
  }

  let content;
  if (LEGAL_ROUTES[route]) {
    // Always reachable, even while a logged-in customer is blocked by
    // the legal-consent gate below, or still "loading".
    content = <LegalPage document={LEGAL_ROUTES[route]} />;
  } else if (customer === undefined) {
    content = <div className="flex min-h-screen items-center justify-center text-slate-500">Loading...</div>;
  } else if (!customer) {
    if (route === "login") content = <Login onAuthed={handleAuthed} />;
    else if (route === "signup") content = <Signup onAuthed={handleAuthed} />;
    else content = <Landing />;
  } else if (!customer.legal_acceptance_current) {
    // Covers both a brand-new customer somehow reaching here before the
    // signup response lands, and - far more commonly - an existing
    // customer who logged in before accepting the current Terms and
    // Privacy Policy. The backend's own require_legal_acceptance
    // dependency enforces this independently on every /api/account/*
    // call, so this isn't just a frontend-only gate.
    content = <LegalConsentGate onAccepted={refreshCustomer} onLogout={handleLogout} />;
  } else {
    content = <AuthedApp customer={customer} onLogout={handleLogout} onCustomerChanged={refreshCustomer} />;
  }

  return (
    <>
      {content}
      <Footer />
      <HowToUseGuide />
    </>
  );
}

// Decides Onboarding vs Dashboard off whether an account already exists,
// rather than off the URL - so a logged-in customer lands in the right
// place on a fresh page load no matter what's in the hash.
function AuthedApp({ customer, onLogout, onCustomerChanged }) {
  const [account, setAccount] = useState(undefined); // undefined = loading, null = doesn't exist yet

  function refreshAccount() {
    setAccount(undefined);
    api
      .get("/account")
      .then(setAccount)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) setAccount(null);
        else setAccount(null);
      });
  }

  useEffect(refreshAccount, []);

  if (account === undefined) {
    return <div className="flex min-h-screen items-center justify-center text-slate-500">Loading...</div>;
  }

  // Onboarding isn't done until the notification channel is linked -
  // trader.py itself refuses to trade an account with no
  // notification_target, so there's no point handing the customer a
  // dashboard for an account that can't trade yet.
  const needsOnboarding = !account || (account.notification_channel === "telegram" && !account.notification_target);
  if (needsOnboarding) {
    return <Onboarding customer={customer} account={account} onLogout={onLogout} onProgress={refreshAccount} />;
  }
  return (
    <Dashboard
      customer={customer}
      account={account}
      onLogout={onLogout}
      onAccountChanged={refreshAccount}
      onCustomerChanged={onCustomerChanged}
    />
  );
}
