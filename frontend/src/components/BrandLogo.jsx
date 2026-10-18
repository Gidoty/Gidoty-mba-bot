// The Metabridge Academy wordmark: an orange "MA" badge, "Metabridge" in
// white/ink and "Academy" in the brand orange - matches the lockup on
// the official Metabridge Academy site. `dark` controls which text
// color reads correctly against the surface it's placed on (the navy
// header vs. a light page background).
export default function BrandLogo({ dark = true, subtitle = "Trading Bot" }) {
  const textColor = dark ? "text-white" : "text-ink";
  const subColor = dark ? "text-white/60" : "text-slate-500";
  return (
    <div className="flex items-center gap-2.5">
      <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent font-serif text-sm font-bold text-white">
        MA
      </span>
      <span className="leading-tight">
        <span className={`block font-serif text-base font-bold ${textColor}`}>
          Metabridge <span className="text-accent">Academy</span>
        </span>
        {subtitle && <span className={`block text-[11px] font-medium tracking-wide ${subColor}`}>{subtitle}</span>}
      </span>
    </div>
  );
}
