import NavIcon from "../components/navIcons.jsx";
import { moreGroups } from "../lib/navigation.js";

// More: every page that has no tab in the bottom bar, in groups. A page of its own (not a pop-up), so the list starts at the top of the screen. Log out is inside Account.
export default function More({ showAdmin = false }) {
  return (
    <div className="mx-auto grid max-w-3xl gap-5">
      {moreGroups(showAdmin).map((group) => (
        <section key={group.title} aria-label={group.title}>
          <h2 className="mb-2 px-1 text-xs font-semibold uppercase tracking-wider text-muted">{group.title}</h2>
          <ul className="overflow-hidden rounded-card border border-line bg-surface">
            {group.items.map((item, i) => (
              <li key={item.route} className={i > 0 ? "border-t border-line" : ""}>
                <a href={`#/${item.route}`} className="flex min-h-[56px] items-center gap-3 px-4 text-sm font-medium text-fg hover:bg-surface-2">
                  <NavIcon route={item.route} className="h-[22px] w-[22px] text-muted" />
                  <span className="min-w-0 flex-1">{item.label}</span>
                  <NavIcon route="chevron" className="h-5 w-5 text-muted" />
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
