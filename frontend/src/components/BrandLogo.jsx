// The Metabridge Bot wordmark: a bridge mark, "Metabridge" and "Bot" in the
// brand amber. `dark` controls which text colour reads on the surface it sits
// on (the navy rail, or a page background that follows the theme).
export default function BrandLogo({ dark = true, subtitle = null }) {
  const textColor = dark ? "text-white" : "text-fg";
  const subColor = dark ? "text-white/60" : "text-muted";
  return (
    <div className="flex items-center gap-2.5">
      <svg viewBox="0 0 32 32" aria-hidden="true" className="h-8 w-8 shrink-0">
        <rect width="32" height="32" rx="8" fill="#F0901E" />
        <path d="M5 22c4-9 8-9 11 0 3-9 7-9 11 0" fill="none" stroke="#1a1205" strokeWidth="2.6" strokeLinecap="round" />
        <path d="M5 25h22" stroke="#1a1205" strokeWidth="2.6" strokeLinecap="round" />
      </svg>
      <span className="leading-tight">
        <span className={`block font-serif text-base font-semibold ${textColor}`}>
          Metabridge <span className="text-accent-light">Bot</span>
        </span>
        {subtitle && <span className={`block text-[11px] font-medium tracking-wide ${subColor}`}>{subtitle}</span>}
      </span>
    </div>
  );
}
