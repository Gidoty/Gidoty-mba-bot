/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#0f172a", light: "#1e293b" },
        accent: { DEFAULT: "#16a34a", light: "#22c55e", dark: "#15803d" },
      },
    },
  },
  plugins: [],
};
