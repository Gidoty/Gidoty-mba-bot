// Shared by Signup.jsx and LegalConsentGate.jsx - never pre-checked by
// a caller; `checked` always starts false in both.
export default function ConsentCheckbox({ checked, onChange, label }) {
  return (
    <label className="flex items-start gap-2 text-sm text-slate-700">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 rounded border-slate-300"
      />
      <span>{label}</span>
    </label>
  );
}
