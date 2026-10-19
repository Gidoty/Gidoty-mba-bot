// The Metabridge Bot wordmark: an orange "MB" badge, "Metabridge" in
// white/ink and "Bot" in the brand orange. `dark` controls which text
// color reads correctly against the surface it's placed on (the navy
// header vs. a light page background).
export default function BrandLogo({ dark = true, subtitle = null }) {
  const textColor = dark ? "text-white" : "text-ink";
  const subColor = dark ? "text-white/60" : "text-slate-500";
  return (
    <div className="flex items-center gap-2.5">
      <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent font-serif text-sm font-bold text-white">
        MB
      </span>
      <span className="leading-tight">
        <span className={`block font-serif text-base font-bold ${textColor}`}>
          Metabridge <span className="text-accent">Bot</span>
        </span>
        {subtitle && <span className={`block text-[11px] font-medium tracking-wide ${subColor}`}>{subtitle}</span>}
      </span>
    </div>
  );
}
