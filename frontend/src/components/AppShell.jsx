import NavDrawer from "./NavDrawer.jsx";

// Wraps every authenticated page: the header/drawer nav, a consistent
// content width, and bottom padding so the floating "How to use" button
// never covers the last card. Each page just renders its own content as
// children - no page re-implements the header.
export default function AppShell({ route, onLogout, botCode, children }) {
  return (
    <div className="min-h-screen bg-slate-50">
      <NavDrawer route={route} onLogout={onLogout} botCode={botCode} />
      <main className="mx-auto max-w-3xl px-4 py-6 pb-20">{children}</main>
    </div>
  );
}
