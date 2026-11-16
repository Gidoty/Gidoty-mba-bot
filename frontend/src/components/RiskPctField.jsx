import { ACK_TEXT, BAND_INFO, acknowledgmentsFor, riskBand, riskProblem } from "../lib/riskBands.js";
import { inputClass } from "./form.jsx";

// Risk per trade: a number input from 0.1 up to the limit the server reports, the band the value is in (label, icon and colour, never colour alone), and the
// acknowledgments a value above 2% (and from 10%) needs. The server refuses a value without them; this only asks.
const TONE_CLASS = {
  pos: "border-l-pos bg-pos-soft text-fg",
  warn: "border-l-warn bg-warn-soft text-fg",
  neg: "border-l-neg bg-neg-soft text-fg",
};

function BandIcon({ kind }) {
  const common = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true, className: "mt-0.5 shrink-0" };
  if (kind === "check") return (<svg {...common}><circle cx="12" cy="12" r="9" /><path d="M8 12.5l2.7 2.7L16 9.5" /></svg>);
  if (kind === "triangle") return (<svg {...common}><path d="M12 3l10 18H2z" /><path d="M12 10v5M12 18h.01" /></svg>);
  return (<svg {...common}><path d="M8 2h8l6 6v8l-6 6H8l-6-6V8z" /><path d="M12 7v6M12 16.5h.01" /></svg>);
}

export function riskFieldProblem(value, limit) {
  return riskProblem(value, limit);
}

export default function RiskPctField({ value, onChange, limit, acks, onAcksChange, label = "Risk per trade (%)", describedBy }) {
  const band = riskBand(value);
  const info = BAND_INFO[band];
  const needed = acknowledgmentsFor(value);
  const problem = value === "" ? null : riskProblem(value, limit);
  return (
    <div>
      <label className="block">
        <span className="text-sm font-medium text-fg">{label}</span>
        <input type="number" min="0.1" max={limit} step="0.1" value={value} onChange={(e) => onChange(e.target.value)} className={inputClass} aria-describedby={describedBy} />
        <span className="mt-1 block text-xs text-muted">From 0.1% up to {limit}%. We recommend 2% or less. The bot sizes every trade from this number and a stop-loss.</span>
      </label>
      {problem && <p role="alert" className="mt-2 text-sm font-medium text-neg">{problem}</p>}
      {!problem && band !== "invalid" && (
        <div role="status" className={`mt-2 flex gap-2 rounded-ctl border border-line border-l-4 px-3 py-2 text-sm font-medium ${TONE_CLASS[info.tone]}`} data-band={band}>
          <BandIcon kind={info.icon} />
          <span>{info.label}</span>
        </div>
      )}
      {!problem && needed.map((key) => (
        <label key={key} className="mt-2 flex items-start gap-2 text-sm text-fg">
          <input type="checkbox" className="mt-1 h-4 w-4" checked={!!acks[key]} onChange={(e) => onAcksChange({ ...acks, [key]: e.target.checked })} />
          <span>{ACK_TEXT[key]}</span>
        </label>
      ))}
    </div>
  );
}

// true when every acknowledgment the value needs is ticked (the Save button waits for this; the server checks again).
export function acksComplete(value, acks) {
  return acknowledgmentsFor(value).every((k) => !!acks[k]);
}
