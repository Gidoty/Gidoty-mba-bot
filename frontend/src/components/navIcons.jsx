// One icon set for the whole navigation (paths in lib/navIconPaths.js): outline only, one stroke weight, no filled shapes, no mixing styles.
import { ICON_PATHS, ICON_STROKE } from "../lib/navIconPaths.js";

export default function NavIcon({ route, className = "h-[22px] w-[22px]" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`${className} shrink-0`}>
      <path d={ICON_PATHS[route] || ICON_PATHS.more} />
    </svg>
  );
}
