// 24x24 line icons for the sidebar and the mobile tab bar, keyed by route.
const PATHS = {
  dashboard: "M3 12l9-8 9 8M5 10v10h5v-6h4v6h5V10",
  positions: "M4 6h16M4 12h16M4 18h10",
  "trade-history": "M12 8v5l3 2M3 12a9 9 0 109-9 9 9 0 00-7 3.4M3 4v4h4",
  performance: "M4 19V5M4 19h16M8 15l4-5 3 3 5-7",
  "bot-settings": "M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M8 15v4",
  "exchange-connection": "M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3A4 4 0 0011 18.7l1-1",
  notifications: "M6 16V11a6 6 0 1112 0v5l1.5 2h-15zM10 20a2 2 0 004 0",
  practice: "M9 3h6M10 3v6l-5 9a2 2 0 001.8 3h10.4a2 2 0 001.8-3l-5-9V3",
  help: "M9.5 9a2.5 2.5 0 115 0c0 1.7-2.5 2-2.5 4M12 17h.01M12 3a9 9 0 100 18 9 9 0 000-18z",
  account: "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0",
  more: "M5 12h.01M12 12h.01M19 12h.01",
};

export default function NavIcon({ route, className = "h-5 w-5" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`${className} shrink-0`}>
      <path d={PATHS[route] || PATHS.more} />
    </svg>
  );
}
