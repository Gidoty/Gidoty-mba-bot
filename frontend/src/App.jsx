import { useEffect, useState } from "react";

import { ApiError, api } from "./api/client.js";
import Dashboard from "./pages/Dashboard.jsx";
import Landing from "./pages/Landing.jsx";
import Login from "./pages/Login.jsx";
import Onboarding from "./pages/Onboarding.jsx";
import Signup from "./pages/Signup.jsx";
import { navigate, useHashRoute } from "./useHashRoute.js";

export default function App() {
  const route = useHashRoute();
  const [customer, setCustomer] = useState(undefined); // undefined = loading, null = logged out

  useEffect(() => {
    api
      .get("/auth/me")
      .then(setCustomer)
      .catch(() => setCustomer(null));
  }, []);

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

  if (customer === undefined) {
    return <div className="flex min-h-screen items-center justify-center text-slate-500">Loading...</div>;
  }

  if (!customer) {
    if (route === "login") return <Login onAuthed={handleAuthed} />;
    if (route === "signup") return <Signup onAuthed={handleAuthed} />;
    return <Landing />;
  }

  return <AuthedApp customer={customer} onLogout={handleLogout} />;
}

// Decides Onboarding vs Dashboard off whether an account already exists,
// rather than off the URL - so a logged-in customer lands in the right
// place on a fresh page load no matter what's in the hash.
function AuthedApp({ customer, onLogout }) {
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
  return <Dashboard customer={customer} account={account} onLogout={onLogout} onAccountChanged={refreshAccount} />;
}
