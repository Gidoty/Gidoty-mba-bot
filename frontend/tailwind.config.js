/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Deep navy - already close to the Metabridge Academy site's own
        // background, kept rather than reinvented. Used for the LIVE
        // banner, drawer nav, and any full-bleed dark section.
        ink: { DEFAULT: "#0f172a", light: "#1e293b", dark: "#0b1220" },
        // The Metabridge brand/action accent (warm amber-orange), sampled
        // from the official site - buttons, links, active nav state,
        // badges. Every existing `accent`-prefixed class across the app
        // picks this up automatically, which is what gives the legal
        // pages, auth forms, and new trading pages one consistent brand
        // color without a file-by-file rename.
        accent: { DEFAULT: "#F0901E", light: "#F6AC4D", dark: "#C97312" },
        // Design 8 tokens. Each reads a CSS variable from src/tokens.css, so a
        // class such as bg-surface or text-pos follows the light/dark theme.
        // Nothing uses these until the shared parts and pages are moved over.
        app: "var(--bg)",
        surface: { DEFAULT: "var(--surface)", 2: "var(--surface-2)" },
        line: { DEFAULT: "var(--line)", strong: "var(--line-strong)" },
        fg: "var(--text)",
        muted: "var(--muted)",
        brand: { DEFAULT: "var(--accent)", ink: "var(--accent-ink)", line: "var(--accent-line)", text: "var(--accent-text)", soft: "var(--accent-soft)" },
        rail: { DEFAULT: "var(--nav)", text: "var(--nav-text)", active: "var(--nav-active)" },
        pos: { DEFAULT: "var(--pos)", soft: "var(--pos-soft)", bar: "var(--pos-bar)" },
        neg: { DEFAULT: "var(--neg)", soft: "var(--neg-soft)", bar: "var(--neg-bar)" },
        warn: { DEFAULT: "var(--warn)", soft: "var(--warn-soft)" },
        info: { DEFAULT: "var(--info)", soft: "var(--info-soft)" },
      },
      borderRadius: { card: "14px", ctl: "10px" },
      fontFamily: {
        // Editorial serif for headlines/wordmark, matching the
        // Metabridge Academy site's own hero typography. Plain sans for
        // everything else (body copy, data, buttons) - a trading
        // dashboard needs to stay legible at small sizes, not decorative.
        serif: ["\"Playfair Display\"", "Georgia", "serif"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        // Figures in tables, tiles and charts: fixed-width digits so columns line up.
        mono: ["\"JetBrains Mono\"", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
