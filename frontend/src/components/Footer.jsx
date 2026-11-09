// Permanent links to the three legal documents - rendered at the App
// level (see App.jsx) so they're reachable on every screen, logged in
// or out, including while a customer is blocked by the legal-consent
// gate. Deliberately only these three - no Refund Policy exists.
export default function Footer({ dark = false }) {
  const link = dark ? "hover:text-white hover:underline" : "hover:text-fg hover:underline";
  return (
    <footer className={`px-4 py-6 text-center text-xs ${dark ? "border-t border-white/10 bg-rail text-slate-400" : "border-t border-line bg-surface text-muted"}`}>
      <a href="#/terms" className={`mx-2 ${link}`}>
        Terms of Service
      </a>
      <a href="#/risk-disclosure" className={`mx-2 ${link}`}>
        Trading Risk Disclosure
      </a>
      <a href="#/privacy" className={`mx-2 ${link}`}>
        Privacy Policy
      </a>
    </footer>
  );
}
