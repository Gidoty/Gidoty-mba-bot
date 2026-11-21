import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { secondaryButton } from "./form.jsx";
import { Card, CardTitle, ErrorBanner } from "./ui.jsx";

// "Your data" on the Account page: ask for a copy of your data or for deletion. A click only RECORDS a request for the operator (POST /api/privacy/requests);
// nothing is exported or deleted by this screen, and the text says so. What is kept after a deletion is stated by the server's reply and in the Privacy Policy.
const LABEL = { export: "Ask for a copy of my data", deletion: "Ask to delete my data" };

export default function DataRequestCard() {
  const [requests, setRequests] = useState([]);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);

  useEffect(() => {
    api.get("/privacy/requests").then(setRequests).catch(() => {});
  }, []);

  async function ask(kind) {
    setBusy(kind);
    setError(null);
    try {
      const row = await api.post("/privacy/requests", { kind });
      setMessage(row.message);
      setRequests((list) => [row, ...list.filter((r) => r.id !== row.id)]);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  const open = (kind) => requests.some((r) => r.kind === kind && r.status === "open");

  return (
    <Card>
      <CardTitle>Your data</CardTitle>
      <p className="text-sm text-muted">
        You can ask for a copy of the data we hold about you, or ask us to delete it. A click here only records your request for the operator; nothing is exported or deleted automatically. Deleting your data does not
        close open trades or revoke your Binance key: do that yourself first. See the{" "}
        <a className="text-brand-text hover:underline" href="#/privacy">Privacy Policy</a> for the records we keep.
      </p>
      <ErrorBanner message={error} />
      <div className="mt-3 flex flex-wrap gap-3">
        {["export", "deletion"].map((kind) => (
          <button key={kind} type="button" disabled={busy !== null || open(kind)} onClick={() => ask(kind)} className={`${secondaryButton} disabled:cursor-not-allowed disabled:opacity-50`}>
            {open(kind) ? `${LABEL[kind]} (requested)` : LABEL[kind]}
          </button>
        ))}
      </div>
      {message && <p className="mt-3 text-sm text-fg">{message}</p>}
    </Card>
  );
}
