import { createContext, useContext, useEffect, useState } from "react";

import { ApiError, api } from "../api/client.js";

// The one thing every authenticated page needs: the trading account
// itself, and the state-machine fields derived from it (mode,
// has_mainnet_credential, live_trading_enabled_at, ...). Trade lists and
// summaries are scoped differently per page (paper vs. live, different
// windows) so each page fetches those locally instead of sharing one
// global copy that would have to serve every scope at once.
const AccountContext = createContext(null);

export function AccountProvider({ customer, onCustomerChanged, children }) {
  const [account, setAccount] = useState(undefined); // undefined = loading, null = doesn't exist yet

  function refreshAccount() {
    setAccount(undefined);
    return api
      .get("/account")
      .then(setAccount)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) setAccount(null);
        else setAccount(null);
      });
  }

  // Not `useEffect(refreshAccount, [])` - refreshAccount returns a
  // Promise so callers can await it, and React treats an effect's own
  // return value as a cleanup function, not a Promise.
  useEffect(() => {
    refreshAccount();
  }, []);

  return (
    <AccountContext.Provider value={{ account, refreshAccount, onCustomerChanged }}>
      {children}
    </AccountContext.Provider>
  );
}

export function useAccount() {
  const ctx = useContext(AccountContext);
  if (!ctx) throw new Error("useAccount must be used within an AccountProvider");
  return ctx;
}
