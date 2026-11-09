import { useThemeChoice } from "../theme.js";
import HelpTip from "./HelpTip.jsx";

// Small shared presentational primitives used across every page. Colours come
// from the design tokens (src/tokens.css), so every part follows light and dark.

export function PageHeader({ title, subtitle, children }) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight text-fg">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
      </div>
      {children}
    </div>
  );
}

export function Card({ children, className = "", style }) {
  return (
    <div className={`min-w-0 rounded-card border border-line bg-surface p-4 ${className}`} style={style}>
      {children}
    </div>
  );
}

// Uppercase label line at the top of a card or chart.
export function CardTitle({ children, right = null }) {
  return (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
      <h2 className="text-xs font-semibold uppercase tracking-wider text-muted">{children}</h2>
      {right}
    </div>
  );
}

// thin: too few trades to read, so every figure in the tile is greyed (the number and its sign stay).
export function StatTile({ label, value, help, basis, thin = false, valueClassName = "text-fg" }) {
  return (
    <div className="min-w-0 rounded-card border border-line bg-surface px-4 py-3.5">
      <dt className="text-xs uppercase tracking-wider text-muted">
        {label}
        {help && <HelpTip text={help} />}
      </dt>
      <dd className={`mt-1 break-words font-mono text-xl font-medium tracking-tight ${thin ? "text-muted [&_*]:!text-muted" : valueClassName}`}>{value}</dd>
      {basis && <p className="mt-0.5 text-xs text-muted">{basis}</p>}
    </div>
  );
}

const TONES = {
  slate: "bg-surface-2 text-muted",
  accent: "bg-brand-soft text-brand-text",
  amber: "bg-warn-soft text-warn",
  red: "bg-red-600 text-white",
  green: "bg-pos-soft text-pos",
  sky: "bg-info-soft text-info",
};

export function Badge({ children, tone = "slate" }) {
  return (
    <span className={`inline-flex items-center rounded-full px-3 py-1 text-xs font-semibold ${TONES[tone]}`}>{children}</span>
  );
}

// Status pill with a leading dot. ok | bad | warn | info | mute.
const PILL = { ok: "bg-pos-soft text-pos", bad: "bg-neg-soft text-neg", warn: "bg-warn-soft text-warn", info: "bg-info-soft text-info", mute: "bg-surface-2 text-muted" };
export function Pill({ children, tone = "mute" }) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-semibold ${PILL[tone]}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
      {children}
    </span>
  );
}

// Win or Loss as a word, so a result never depends on colour alone.
export function ResultPill({ pnl }) {
  if (pnl === null || pnl === undefined) return <Pill>Open</Pill>;
  return pnl > 0 ? <Pill tone="ok">Win</Pill> : pnl < 0 ? <Pill tone="bad">Loss</Pill> : <Pill>Flat</Pill>;
}

export function PnlText({ value, className = "", unit = "" }) {
  if (value === null || value === undefined) return <span className={`text-muted ${className}`}>-</span>;
  const tone = value > 0 ? "text-pos" : value < 0 ? "text-neg" : "text-muted";
  return (
    <span className={`${tone} ${className}`}>
      {value > 0 ? "+" : ""}
      {value.toFixed(2)}
      {unit ? ` ${unit}` : ""}
    </span>
  );
}

// Boxed message. tone: warn (default) | info.
export function Notice({ children, tone = "warn" }) {
  const edge = tone === "info" ? "border-l-info bg-info-soft" : "border-l-warn bg-warn-soft";
  return (
    <div role="note" className={`rounded-ctl border border-line border-l-4 px-3.5 py-3 text-sm text-fg ${edge}`}>
      {children}
    </div>
  );
}

// "Results are not proven" - shown on every page that shows results.
export function UnprovenNotice({ closedCount = null, paper = false }) {
  const record =
    !closedCount ? null : (
      <> This account has {closedCount} closed {closedCount === 1 ? "trade" : "trades"}, which is a short record.</>
    );
  return (
    <Notice>
      <strong className="font-semibold">Results are not proven.</strong>
      {record}{" "}
      {paper
        ? "Practice trades are simulated: no order is sent, and there are no fees or slippage, so real trading can differ. Past results do not predict future ones."
        : "Figures are before exchange fees and funding, so what you keep is lower. Past results do not predict future ones."}
    </Notice>
  );
}

// 1 to 8 closed trades: too few to read a win rate, average or drawdown.
export function ThinNotice({ closedCount }) {
  return (
    <Notice tone="info">
      <strong className="font-semibold">Not enough trades to read.</strong> {closedCount} closed{" "}
      {closedCount === 1 ? "trade is" : "trades are"} too few for a win rate, average or drawdown to mean anything. Treat them as a first look, not a result.
    </Notice>
  );
}

// Segmented control. options: [{ value, label }].
export function Seg({ options, value, onChange, label }) {
  return (
    <div role="group" aria-label={label} className="inline-flex overflow-hidden rounded-ctl border border-line-strong bg-surface">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={`min-h-9 px-3 text-sm ${value === o.value ? "bg-brand-soft font-semibold text-brand-text" : "font-medium text-muted hover:bg-surface-2"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function ThemeSwitch() {
  const [choice, setChoice] = useThemeChoice();
  return (
    <Seg
      label="Colour theme"
      value={choice}
      onChange={setChoice}
      options={[
        { value: "auto", label: "Auto" },
        { value: "light", label: "Light" },
        { value: "dark", label: "Dark" },
      ]}
    />
  );
}

export function EmptyState({ title, body }) {
  return (
    <div className="rounded-ctl border border-dashed border-line-strong bg-surface px-4 py-8 text-center">
      <p className="text-sm font-medium text-fg">{title}</p>
      {body && <p className="mt-1 text-xs text-muted">{body}</p>}
    </div>
  );
}

export function LoadingBlock() {
  return (
    <div className="space-y-3" role="status" aria-label="Loading">
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-16 animate-pulse rounded-card bg-surface-2" />
      ))}
    </div>
  );
}

export function ErrorBanner({ message }) {
  if (!message) return null;
  return (
    <p role="alert" className="mb-4 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm text-fg">
      {message}
    </p>
  );
}
