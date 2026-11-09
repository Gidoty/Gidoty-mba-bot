// Form pieces on the design tokens, shared by the settings pages. Controls are 44 px tall,
// their borders meet the 3:1 contrast rule, and every control sits inside its label so a tap on
// the text focuses it.
export const inputClass =
  "mt-1.5 min-h-[44px] w-full rounded-ctl border border-line-strong bg-surface px-3.5 text-sm text-fg placeholder:text-muted disabled:bg-surface-2 disabled:text-muted";

export const primaryButton =
  "inline-flex min-h-[44px] items-center justify-center rounded-ctl bg-brand px-5 text-sm font-semibold text-brand-ink hover:brightness-105 disabled:cursor-not-allowed disabled:opacity-40";

export const secondaryButton =
  "inline-flex min-h-[44px] items-center justify-center rounded-ctl border border-line-strong bg-surface px-4 text-sm font-semibold text-fg hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-60";

export const linkButton = "text-sm font-semibold text-brand-text hover:underline";

export function FormField({ label, hint, children, className = "" }) {
  return (
    <label className={`block ${className}`}>
      <span className="text-sm font-medium text-fg">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

// Short confirmation after a save. Announced to screen readers.
export function SavedNote({ show, children = "Saved." }) {
  if (!show) return null;
  return (
    <p role="status" className="mt-3 text-sm font-medium text-pos">
      {children}
    </p>
  );
}
