import { navigate } from "../useHashRoute.js";
import BrandLogo from "./BrandLogo.jsx";

// Shared by Login and Signup: the brand panel on the left from 900 px (a short header on a phone)
// and the form on the right. The pattern is the brand mark's arcs, not a chart, so nothing here
// suggests a result.
export default function AuthLayout({ heading, blurb, children }) {
  return (
    <div className="min-h-screen bg-app text-fg shell:grid shell:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <section className="flex flex-col justify-between gap-8 bg-rail px-4 py-6 shell:px-10 shell:py-10">
        <button type="button" onClick={() => navigate("landing")} className="w-fit text-left" aria-label="Metabridge Bot home">
          <BrandLogo />
        </button>
        <div>
          <h2 className="max-w-[18ch] text-2xl font-semibold leading-tight tracking-tight text-white shell:text-3xl">{heading}</h2>
          <p className="mt-3 max-w-[44ch] text-sm text-rail-text">{blurb}</p>
        </div>
        <svg viewBox="0 0 400 120" aria-hidden="true" className="hidden h-auto max-h-44 w-full shell:block">
          <g fill="none" stroke="#253352" strokeWidth="2">
            <path d="M0 110c40-70 80-70 120 0s80 70 120 0 80-70 120 0 40 35 40 35" />
            <path d="M0 80c40-70 80-70 120 0s80 70 120 0 80-70 120 0 40 35 40 35" opacity="0.6" />
            <path d="M0 50c40-70 80-70 120 0s80 70 120 0 80-70 120 0 40 35 40 35" opacity="0.3" />
          </g>
        </svg>
      </section>

      <section className="relative flex items-center justify-center px-4 pb-24 pt-10 shell:px-10 shell:pb-10">
        <div className="mt-8 w-full max-w-sm space-y-4 shell:mt-0">{children}</div>
      </section>
    </div>
  );
}
