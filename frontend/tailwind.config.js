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
      },
      fontFamily: {
        // Editorial serif for headlines/wordmark, matching the
        // Metabridge Academy site's own hero typography. Plain sans for
        // everything else (body copy, data, buttons) - a trading
        // dashboard needs to stay legible at small sizes, not decorative.
        serif: ["\"Playfair Display\"", "Georgia", "serif"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
