import HelpTip from "./HelpTip.jsx";

// Small shared presentational primitives used across every page in the
// redesign - kept in one file since each is a few lines and splitting
// them out would just mean more imports for no real benefit.

export function PageHeader({ title, subtitle }) {
  return (
    <div className="mb-5">
      <h1 className="text-xl font-semibold text-ink">{title}</h1>
      {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
    </div>
  );
}

export function Card({ children, className = "" }) {
  return <div className={`rounded-lg border border-slate-200 bg-white p-4 ${className}`}>{children}</div>;
}

export function StatTile({ label, value, help, valueClassName = "text-ink" }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <dt className="text-xs uppercase tracking-wide text-slate-500">
        {label}
        {help && <HelpTip text={help} />}
      </dt>
      <dd className={`mt-1 text-lg font-semibold ${valueClassName}`}>{value}</dd>
    </div>
  );
}

export function Badge({ children, tone = "slate" }) {
  const tones = {
    slate: "bg-slate-100 text-slate-600",
    accent: "bg-accent/10 text-accent-dark",
    amber: "bg-amber-100 text-amber-800",
    red: "bg-red-600 text-white",
    green: "bg-emerald-100 text-emerald-800",
    sky: "bg-sky-100 text-sky-800",
  };
  return (
    <span className={`inline-flex items-center rounded-full px-3 py-1 text-xs font-semibold ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function PnlText({ value, className = "" }) {
  if (value === null || value === undefined) return <span className={`text-slate-400 ${className}`}>-</span>;
  const tone = value > 0 ? "text-green-600" : value < 0 ? "text-red-600" : "text-slate-500";
  return (
    <span className={`${tone} ${className}`}>
      {value > 0 ? "+" : ""}
      {value.toFixed(2)}
    </span>
  );
}

export function EmptyState({ title, body }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white px-4 py-8 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {body && <p className="mt-1 text-xs text-slate-500">{body}</p>}
    </div>
  );
}

export function LoadingBlock() {
  return (
    <div className="space-y-3">
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-16 animate-pulse rounded-lg bg-slate-100" />
      ))}
    </div>
  );
}

export function ErrorBanner({ message }) {
  if (!message) return null;
  return <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{message}</p>;
}
