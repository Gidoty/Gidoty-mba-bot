import { useEffect, useState } from "react";

import { api } from "./api/client.js";
import AppShell from "./components/AppShell.jsx";
import Footer from "./components/Footer.jsx";
import HowToUseGuide from "./components/HowToUseGuide.jsx";
import LegalConsentGate from "./components/LegalConsentGate.jsx";
import { AccountProvider, useAccount } from "./context/AccountContext.jsx";
import { PRIVACY_POLICY, RISK_DISCLOSURE, TERMS_OF_SERVICE } from "./legalContent.js";
import Account from "./pages/Account.jsx";
import BotSettings from "./pages/BotSettings.jsx";
import DashboardRouter from "./pages/DashboardRouter.jsx";
import ExchangeConnection from "./pages/ExchangeConnection.jsx";
import Help from "./pages/Help.jsx";
import Landing from "./pages/Landing.jsx";
import LegalPage from "./pages/LegalPage.jsx";
import Login from "./pages/Login.jsx";
import Notifications from "./pages/Notifications.jsx";
import Onboarding from "./pages/Onboarding.jsx";
import Performance from "./pages/Performance.jsx";
import Positions from "./pages/Positions.jsx";
import Practice from "./pages/Practice.jsx";
import Signup from "./pages/Signup.jsx";
import TradeHistory from "./pages/TradeHistory.jsx";
import { navigate, useHashRoute } from "./useHashRoute.js";

// Static legal pages - reachable regardless of auth state, and
// deliberately checked before anything that depends on login/consent.
const LEGAL_ROUTES = {
  terms: TERMS_OF_SERVICE,
  "risk-disclosure": RISK_DISCLOSURE,
  privacy: PRIVACY_POLICY,
};

// The approved drawer nav, mapped to their page components. "dashboard"
// is handled separately by DashboardRouter, which picks Testnet /
// Activation / Connect Binance / Ready-for-Live / Live off the
// account's own state rather than being a single fixed page.
const NAV_PAGES = {
  positions: Positions,
  "trade-history": TradeHistory,
  performance: Performance,
  "bot-settings": BotSettings,
  "exchange-connection": ExchangeConnection,
  notifications: Notifications,
  practice: Practice,
  help: Help,
  account: Account,
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

  // Not `useEffect(refreshCustomer, [])` - refreshCustomer returns a
  // Promise (so LegalConsentGate/ActivationFlow can await it), and
  // React treats an effect's return value as a cleanup function, not a
  // Promise. Wrapping it so the effect callback itself returns nothing.
  useEffect(() => {
    refreshCustomer();
  }, []);

  function handleAuthed(nextCustomer) {
    setCustomer(nextCustomer);
    navigate("dashboard");
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
    content = (
      <AccountProvider customer={customer} onCustomerChanged={refreshCustomer}>
        <AuthedApp customer={customer} onLogout={handleLogout} onCustomerChanged={refreshCustomer} route={route} />
      </AccountProvider>
    );
  }

  return (
    <>
      {content}
      <Footer />
      <HowToUseGuide />
    </>
  );
}

// Decides Onboarding vs. the navigable app off whether an account
// already exists and is linked, rather than off the URL - so a
// logged-in customer lands in the right place on a fresh page load no
// matter what's in the hash.
function AuthedApp({ customer, onLogout, onCustomerChanged, route }) {
  const { account, refreshAccount } = useAccount();

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

  const Page = NAV_PAGES[route];
  const activeRoute = Page ? route : "dashboard";

  return (
    <AppShell route={activeRoute} onLogout={onLogout} botCode={account.bot_code}>
      {Page ? (
        <Page customer={customer} onLogout={onLogout} onCustomerChanged={onCustomerChanged} />
      ) : (
        <DashboardRouter customer={customer} onCustomerChanged={onCustomerChanged} />
      )}
    </AppShell>
  );
}
