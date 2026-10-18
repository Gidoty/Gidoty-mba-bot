// Permanent links to the three legal documents - rendered at the App
// level (see App.jsx) so they're reachable on every screen, logged in
// or out, including while a customer is blocked by the legal-consent
// gate. Deliberately only these three - no Refund Policy exists.
export default function Footer() {
  return (
    <footer className="border-t border-slate-200 bg-white px-4 py-6 text-center text-xs text-slate-500">
      <a href="#/terms" className="mx-2 hover:text-ink hover:underline">
        Terms of Service
      </a>
      <a href="#/risk-disclosure" className="mx-2 hover:text-ink hover:underline">
        Trading Risk Disclosure
      </a>
      <a href="#/privacy" className="mx-2 hover:text-ink hover:underline">
        Privacy Policy
      </a>
    </footer>
  );
}
