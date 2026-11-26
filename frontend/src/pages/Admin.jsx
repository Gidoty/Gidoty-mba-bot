import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client.js";
import { Card, CardTitle, ErrorBanner, LoadingBlock, PageHeader, Pill, Seg, StatTile } from "../components/ui.jsx";
import { FormField, inputClass, primaryButton, secondaryButton } from "../components/form.jsx";
import { ACTIONS, buildRequest, settingBadge, settingProblem } from "../lib/adminActions.js";
import { KILL_SWITCH_TEXT, MODE_TEXT, REASON_MAX, REFRESH_MS, WINDOWS, adminStage, ageText, codeProblem, killSwitchTone, pageCount, reasonProblem, resultLine, winRate } from "../lib/admin.js";

// Read-only admin dashboard. It shows what the server sends and nothing else: no credentials, no network address, no full email unless the admin asks for one with a reason. The page is reachable only when
// GET /api/admin/me says the signed-in customer is an admin; every panel asks the server again, and a 401 sends the admin back to the sign-in step (the admin session lasts 15 minutes from the last request).

export function useAdminMe() {
  const [me, setMe] = useState(undefined);
  const load = useCallback(() => api.get("/admin/me").then(setMe, () => setMe(null)), []);
  useEffect(() => {
    load();
  }, [load]);
  return { me, reload: load };
}

function usePanel(path, onAuthLost) {
  const [state, setState] = useState({ status: "loading", data: null });
  const lost = useRef(onAuthLost);
  lost.current = onAuthLost;
  const load = useCallback(async () => {
    try {
      setState({ status: "ready", data: await api.get(path) });
    } catch (err) {
      if (err.status === 401) lost.current();
      else setState((s) => ({ status: "error", data: s.data, message: err.message }));
    }
  }, [path]);
  useEffect(() => {
    setState((s) => ({ ...s, status: s.data ? "ready" : "loading" }));
    load();
    const id = setInterval(() => document.visibilityState === "visible" && load(), REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);
  return { ...state, reload: load };
}

function PanelBody({ panel, children }) {
  if (panel.status === "loading") return <LoadingBlock />;
  if (!panel.data) return <ErrorBanner message={panel.message || "Could not load this panel."} />;
  return (
    <>
      {panel.status === "error" && <ErrorBanner message={`Showing the last answer. ${panel.message || ""}`} />}
      {children(panel.data)}
    </>
  );
}

function SignIn({ onSignedIn }) {
  const [code, setCode] = useState("");
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    const p = codeProblem(code);
    setProblem(p);
    if (p) return;
    setBusy(true);
    try {
      await api.post("/admin/login", { code: code.trim() });
      setCode("");
      onSignedIn();
    } catch (err) {
      setProblem(err.message);
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-md">
      <PageHeader title="Admin" subtitle="Enter the code from your authenticator app to open the admin dashboard." />
      <Card>
        <form onSubmit={submit} className="grid gap-3" noValidate>
          <FormField label="6-digit code or recovery code">
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              autoComplete="one-time-code"
              autoCapitalize="off"
              spellCheck={false}
              maxLength={24}
              className={inputClass}
              aria-invalid={problem ? "true" : undefined}
            />
          </FormField>
          {problem && <p role="alert" className="rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm font-medium text-fg">{problem}</p>}
          <div>
            <button type="submit" disabled={busy} className={primaryButton}>
              {busy ? "Checking..." : "Open admin dashboard"}
            </button>
          </div>
        </form>
      </Card>
    </div>
  );
}

function Stat({ label, value, tone }) {
  return <StatTile label={label} value={value ?? "-"} valueClassName={tone === "bad" ? "text-neg" : "text-fg"} />;
}

function CustomersPanel({ onAuthLost }) {
  const p = usePanel("/admin/customers/summary", onAuthLost);
  return (
    <Card>
      <CardTitle>Customers</CardTitle>
      <PanelBody panel={p}>
        {(d) => (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Stat label="Total" value={d.total} />
            <Stat label="Active" value={d.active} />
            <Stat label="Paid" value={d.paid} />
            <Stat label="New, 7 days" value={d.new_7_days} />
            <Stat label="New, 30 days" value={d.new_30_days} />
          </div>
        )}
      </PanelBody>
    </Card>
  );
}

function BotsPanel({ onAuthLost }) {
  const p = usePanel("/admin/bots", onAuthLost);
  return (
    <Card>
      <CardTitle>Bots by mode</CardTitle>
      <PanelBody panel={p}>
        {(d) => (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            {["practice", "live", "live_stopped", "inactive"].map((k) => (
              <Stat key={k} label={MODE_TEXT[k]} value={d[k]} />
            ))}
            <Stat label="All bots" value={d.total} />
          </div>
        )}
      </PanelBody>
    </Card>
  );
}

function HealthPanel({ onAuthLost }) {
  const p = usePanel("/admin/health", onAuthLost);
  return (
    <Card>
      <CardTitle right={<button type="button" onClick={p.reload} className={secondaryButton}>Refresh</button>}>Health</CardTitle>
      <PanelBody panel={p}>
        {(d) => (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 shell:grid-cols-4">
              <Stat label="Oldest last scan" value={ageText(d.oldest_last_scan_age_seconds)} />
              <Stat label="Bots with an error now" value={d.bots_with_error_now} tone={d.bots_with_error_now ? "bad" : undefined} />
              <Stat label="Not scanned in 15 min" value={d.bots_not_scanned_15_min} tone={d.bots_not_scanned_15_min ? "bad" : undefined} />
              <Stat label="Open trades" value={d.open_trades} />
              <Stat label="Protection not healthy" value={d.open_trades_protection_not_healthy} tone={d.open_trades_protection_not_healthy ? "bad" : undefined} />
              <Stat label="Last deploy" value={d.last_deploy_commit || "unknown"} />
            </div>
            <p className={`mt-3 text-sm font-medium ${killSwitchTone(d.kill_switch) === "ok" ? "text-fg" : "text-neg"}`}>Kill switch: {KILL_SWITCH_TEXT[d.kill_switch] || d.kill_switch}</p>
          </>
        )}
      </PanelBody>
    </Card>
  );
}

function GroupRow({ g }) {
  const wr = winRate(g);
  return (
    <li className="grid gap-0.5 border-t border-line py-2 text-sm first:border-t-0">
      <span className="flex flex-wrap items-center gap-x-2">
        <Pill tone={g.mode === "live" ? "info" : "mute"}>{g.mode === "paper" ? "practice" : g.mode}</Pill>
        <span className="font-medium text-fg">{g.strategy}</span>
        <span className="text-muted">{g.closed_trades} trades{wr !== null ? `, ${wr}% won` : ""}</span>
      </span>
      <span className="text-fg">{resultLine(g)}</span>
      <span className="text-xs text-muted">{g.label}</span>
    </li>
  );
}

function ResultsPanel({ onAuthLost }) {
  const [win, setWin] = useState("30");
  const p = usePanel(`/admin/results?window_days=${win}`, onAuthLost);
  return (
    <Card>
      <CardTitle right={<Seg label="Window" value={win} onChange={setWin} options={WINDOWS} />}>Results per bot (closed trades)</CardTitle>
      <PanelBody panel={p}>
        {(d) => (
          <>
            <p className="mb-3 text-xs text-muted">Live and practice are never added together. Figures are before exchange fees unless the label says net, which needs every fee known. Practice is simulated. {d.funding_note}</p>
            <h3 className="mb-1 text-sm font-semibold text-fg">All bots</h3>
            {d.totals.length === 0 ? <p className="text-sm text-muted">No closed trades in this window.</p> : <ul>{d.totals.map((g) => <GroupRow key={`${g.mode}-${g.strategy}`} g={g} />)}</ul>}
            {d.bots.length > 0 && <h3 className="mb-1 mt-4 text-sm font-semibold text-fg">{d.bots.length === 1 ? "The bot" : `The ${d.bots.length} bots`} with the most closed trades</h3>}
            <div className="grid gap-3 md:grid-cols-2">
              {d.bots.map((b) => (
                <div key={b.bot_code} className="min-w-0 rounded-ctl border border-line px-3 py-2">
                  <p className="text-sm font-semibold text-fg">{b.bot_code} <span className="font-normal text-muted">{b.pair} {b.market_type}</span></p>
                  <ul>{b.groups.map((g) => <GroupRow key={`${g.mode}-${g.strategy}`} g={g} />)}</ul>
                </div>
              ))}
            </div>
          </>
        )}
      </PanelBody>
    </Card>
  );
}

function RevealForm({ refId, onDone }) {
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);
  async function submit(e) {
    e.preventDefault();
    const bad = reasonProblem(reason);
    setProblem(bad);
    if (bad) return;
    setBusy(true);
    try {
      const r = await api.post("/admin/customers/reveal", { ref: refId, reason: reason.trim() });
      onDone(r.email, r.reveals_left_today);
    } catch (err) {
      setProblem(err.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="mt-2 grid gap-2" noValidate>
      <label className="text-xs text-muted" htmlFor={`why-${refId}`}>Why do you need this email? (5 to {REASON_MAX} characters, written to the audit log)</label>
      <input id={`why-${refId}`} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={REASON_MAX} className={inputClass} />
      {problem && <p role="alert" className="text-sm font-medium text-neg">{problem}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={busy} className={primaryButton}>{busy ? "Checking..." : "Show email once"}</button>
        <button type="button" onClick={() => onDone(null)} className={secondaryButton}>Cancel</button>
      </div>
    </form>
  );
}

function CustomerList({ onAuthLost }) {
  const [page, setPage] = useState(1);
  const p = usePanel(`/admin/customers?page=${page}`, onAuthLost);
  const [open, setOpen] = useState(null); // the ref whose reveal form is open
  const [shown, setShown] = useState(null); // { ref, email, left } - kept for 20 seconds, then gone
  useEffect(() => {
    if (!shown) return undefined;
    const id = setTimeout(() => setShown(null), 20_000);
    return () => clearTimeout(id);
  }, [shown]);
  return (
    <Card>
      <CardTitle>Customers (emails masked)</CardTitle>
      <PanelBody panel={p}>
        {(d) => (
          <>
            <ul className="grid gap-2">
              {d.items.map((c) => (
                <li key={c.ref || `${c.masked_email}-${c.created_at}`} className="min-w-0 rounded-ctl border border-line px-3 py-2 text-sm">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className="font-medium text-fg">{c.masked_email}</span>
                    {c.mode && <Pill tone={c.mode === "live" ? "info" : "mute"}>{MODE_TEXT[c.mode]}</Pill>}
                    {c.paid && <Pill>paid</Pill>}
                    {!c.customer_active && <Pill>inactive</Pill>}
                    {c.has_error && <Pill tone="info">error</Pill>}
                  </div>
                  <p className="text-xs text-muted">
                    {c.account_prefix ? `bot ${c.account_prefix}` : "no bot yet"} {c.pair || ""} {c.market_type || ""} - scanned {ageText(c.last_scan_age_seconds)} - joined {c.created_at ? new Date(c.created_at).toLocaleDateString() : "?"}
                  </p>
                  {c.ref && shown?.ref === c.ref && <p className="mt-1 break-all text-sm font-semibold text-fg" role="status">{shown.email} <span className="font-normal text-muted">(shown once; {shown.left} reveals left today)</span></p>}
                  {c.ref && open === c.ref ? (
                    <RevealForm refId={c.ref} onDone={(email, left) => { setOpen(null); if (email) setShown({ ref: c.ref, email, left }); }} />
                  ) : (
                    c.ref && <button type="button" onClick={() => setOpen(c.ref)} className="mt-1 min-h-[44px] text-sm font-semibold text-brand-text hover:underline">Show full email</button>
                  )}
                </li>
              ))}
            </ul>
            <div className="mt-3 flex items-center justify-between gap-3 text-sm">
              <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} className={secondaryButton}>Previous</button>
              <span className="text-muted">Page {d.page} of {pageCount(d.total, d.page_size)} ({d.total} customers)</span>
              <button type="button" disabled={page >= pageCount(d.total, d.page_size)} onClick={() => setPage(page + 1)} className={secondaryButton}>Next</button>
            </div>
          </>
        )}
      </PanelBody>
    </Card>
  );
}

function SkipsPanel({ onAuthLost }) {
  const p = usePanel("/admin/skipped-pairs", onAuthLost);
  return (
    <Card>
      <CardTitle>Skipped pairs, last 7 days</CardTitle>
      <PanelBody panel={p}>
        {(d) => (d.reasons.length === 0 ? <p className="text-sm text-muted">No pair was skipped.</p> : (
          <ul className="grid gap-1 text-sm">
            {d.reasons.map((r) => <li key={r.reason} className="text-fg"><span className="font-medium">{r.reason}</span>: {r.skips} skips on {r.accounts} bots</li>)}
          </ul>
        ))}
      </PanelBody>
    </Card>
  );
}

function BillingPanel({ onAuthLost }) {
  const p = usePanel("/admin/billing", onAuthLost);
  return (
    <Card>
      <CardTitle>Billing (read only)</CardTitle>
      <PanelBody panel={p}>
        {(d) => (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
              <Stat label="Active" value={d.subscriptions.active} />
              <Stat label="Grace" value={d.subscriptions.grace} />
              <Stat label="Expired" value={d.subscriptions.expired} />
              <Stat label="Cancelled" value={d.subscriptions.cancelled} />
              <Stat label="Expiring in 7 days" value={d.expiring_in_7_days} />
            </div>
            <p className="mt-3 text-sm text-fg">Failed or mismatched payments, last 30 days: <strong>{d.failed_or_mismatched_30_days}</strong></p>
            <h3 className="mb-1 mt-3 text-sm font-semibold text-fg">Payments, last 30 days</h3>
            {d.payments_30_days.length === 0 ? <p className="text-sm text-muted">None.</p> : (
              <ul className="grid gap-1 text-sm">
                {d.payments_30_days.map((x) => <li key={`${x.provider}-${x.status}-${x.currency}`} className="text-fg">{x.provider}, {x.status}: {x.count} ({x.amount_minor} {x.currency}, minor units)</li>)}
              </ul>
            )}
            <h3 className="mb-1 mt-3 text-sm font-semibold text-fg">Billing switches</h3>
            <ul className="grid gap-1 text-sm text-fg">
              {Object.entries(d.switches).map(([k, v]) => <li key={k}>{k}: <strong>{String(v)}</strong></li>)}
            </ul>
          </>
        )}
      </PanelBody>
    </Card>
  );
}

function ActionsPanel({ onAuthLost }) {
  const settings = usePanel("/admin/settings", onAuthLost);
  const changes = usePanel("/admin/changes", onAuthLost);
  const [action, setAction] = useState("pause");
  const [form, setForm] = useState({ target: "", reason: "", code: "", days: "30", note: "" });
  const [edit, setEdit] = useState({ key: "", value: "", reason: "", code: "" });
  const [msg, setMsg] = useState(null);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const act = ACTIONS.find((a) => a.id === action);
  const run = async (req, after) => {
    if (req.error) return setMsg({ bad: true, text: req.error });
    try {
      const r = await api.post(req.path, req.body);
      setMsg({ bad: false, text: `Done: ${r.action}${r.restart_required ? " (takes effect at the next restart)" : ""}.` });
      after();
    } catch (err) {
      if (err.status === 401 && /sign-in/.test(err.message || "")) onAuthLost();
      setMsg({ bad: true, text: err.message });
    }
  };
  const data = settings.data;
  const def = data?.settings.find((x) => x.key === edit.key);
  return (
    <Card>
      <CardTitle>Settings and actions</CardTitle>
      {data && !data.actions_enabled && <p className="text-sm text-muted">Writing is switched off on the server (ADMIN_ACTIONS_ENABLED). You can read everything here.</p>}
      {msg && <p className={`text-sm ${msg.bad ? "text-bad" : "text-ok"}`} role="status">{msg.text}</p>}
      <PanelBody panel={settings}>
        {(d) => (
          <ul className="grid gap-1 text-sm text-fg">
            {d.settings.map((x) => (
              <li key={x.key}><span className="font-medium">{x.key}</span>: <strong>{String(x.effective_value)}</strong> <span className="text-muted">({settingBadge(x)}; {x.description})</span></li>
            ))}
          </ul>
        )}
      </PanelBody>
      <h3 className="mb-1 mt-3 text-sm font-semibold text-fg">Change a setting</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        <FormField label="Setting"><select className={inputClass} value={edit.key} onChange={(e) => setEdit({ ...edit, key: e.target.value })}><option value="">Choose</option>{(data?.settings || []).map((x) => <option key={x.key} value={x.key}>{x.key}</option>)}</select></FormField>
        <FormField label="New value"><input className={inputClass} value={edit.value} onChange={(e) => setEdit({ ...edit, value: e.target.value })} /></FormField>
        <FormField label="Reason"><input className={inputClass} value={edit.reason} maxLength={REASON_MAX} onChange={(e) => setEdit({ ...edit, reason: e.target.value })} /></FormField>
        <FormField label="Authenticator code"><input className={inputClass} value={edit.code} autoComplete="one-time-code" onChange={(e) => setEdit({ ...edit, code: e.target.value })} /></FormField>
      </div>
      <button type="button" className={primaryButton} onClick={() => {
        const problem = settingProblem(def, edit.value);
        if (problem) return setMsg({ bad: true, text: problem });
        run({ path: "/admin/settings/set", body: { key: edit.key, value: edit.value.trim(), reason: edit.reason.trim(), code: edit.code.trim() } }, () => { settings.reload(); changes.reload(); setEdit({ ...edit, code: "" }); });
      }}>Save setting</button>
      <h3 className="mb-1 mt-4 text-sm font-semibold text-fg">Act on a customer or request</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        <FormField label="Action"><select className={inputClass} value={action} onChange={(e) => setAction(e.target.value)}>{ACTIONS.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}</select></FormField>
        {act.target !== "none" && <FormField label={act.target === "ref" ? "Customer ref (from the customer list)" : "Request id"}><input className={inputClass} value={form.target} onChange={set("target")} /></FormField>}
        {act.days && <FormField label="Days"><input className={inputClass} value={form.days} onChange={set("days")} /></FormField>}
        {act.note && <FormField label="Note"><input className={inputClass} value={form.note} maxLength={500} onChange={set("note")} /></FormField>}
        <FormField label="Reason"><input className={inputClass} value={form.reason} maxLength={REASON_MAX} onChange={set("reason")} /></FormField>
        <FormField label="Authenticator code"><input className={inputClass} value={form.code} autoComplete="one-time-code" onChange={set("code")} /></FormField>
      </div>
      <button type="button" className={primaryButton} onClick={() => run(buildRequest(action, form), () => { changes.reload(); setForm({ ...form, code: "" }); })}>Do it</button>
      <p className="mt-2 text-xs text-muted">None of these places, closes or changes a trade or an exchange order. Open trades stay managed.</p>
      <h3 className="mb-1 mt-4 text-sm font-semibold text-fg">Recent changes</h3>
      <PanelBody panel={changes}>
        {(d) => (d.changes.length === 0 ? <p className="text-sm text-muted">None yet.</p> : (
          <ul className="grid gap-1 text-sm text-fg">
            {d.changes.slice(0, 20).map((c, i) => <li key={i}>{new Date(c.at).toLocaleString()}: {c.action} {c.target} ({c.old ?? "-"} to {c.new ?? "-"}), {c.reason}</li>)}
          </ul>
        ))}
      </PanelBody>
    </Card>
  );
}

export default function Admin() {
  const { me, reload } = useAdminMe();
  const stage = adminStage(me);
  if (me === undefined) return <LoadingBlock />;
  if (stage === "hidden") return <p className="text-sm text-muted">This page is not available.</p>;
  if (stage === "login") return <SignIn onSignedIn={reload} />;
  const lost = () => reload();
  return (
    <div className="grid gap-4">
      <PageHeader title="Admin" subtitle="The panels are read only. Changes need a reason and a fresh code, and every request is written to the audit log. The session ends after 15 minutes without a request.">
        <button type="button" onClick={() => api.post("/admin/logout").then(reload, reload)} className={secondaryButton}>Sign out of admin</button>
      </PageHeader>
      <CustomersPanel onAuthLost={lost} />
      <BotsPanel onAuthLost={lost} />
      <HealthPanel onAuthLost={lost} />
      <ResultsPanel onAuthLost={lost} />
      <BillingPanel onAuthLost={lost} />
      <SkipsPanel onAuthLost={lost} />
      <CustomerList onAuthLost={lost} />
      <ActionsPanel onAuthLost={lost} />
    </div>
  );
}
