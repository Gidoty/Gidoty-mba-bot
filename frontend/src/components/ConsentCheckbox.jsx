// Shared by Signup.jsx and LegalConsentGate.jsx - never pre-checked by
// a caller; `checked` always starts false in both.
export default function ConsentCheckbox({ checked, onChange, label }) {
  return (
    <label className="flex items-start gap-3 text-sm text-fg">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-5 w-5 shrink-0 rounded border-line-strong accent-brand-line"
      />
      <span>{label}</span>
    </label>
  );
}
