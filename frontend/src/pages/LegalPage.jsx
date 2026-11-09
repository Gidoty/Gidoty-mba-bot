// Renders any one of the three documents from legalContent.js. Plain
// read-only page - reachable before and after login, and from the
// legal-consent gate while a customer is blocked, so it never depends
// on auth state.
export default function LegalPage({ document }) {
  return (
    <div className="min-h-screen bg-app px-4 py-10">
      <div className="mx-auto max-w-2xl">
        <button
          onClick={() => window.history.back()}
          className="mb-6 text-sm font-medium text-brand-text hover:underline"
        >
          ← Back
        </button>
        <div className="rounded-lg border border-line bg-surface p-8 shadow-sm">
          <h1 className="text-xl font-semibold text-fg">{document.title}</h1>
          <p className="mt-1 text-xs text-muted">Effective Date: {document.effectiveDate}</p>

          <div className="mt-6 space-y-2 text-sm leading-relaxed text-fg">
            {document.intro.map((p, i) => (
              <p key={i}>{p}</p>
            ))}
          </div>

          <div className="mt-6 space-y-6">
            {document.sections.map((section, i) => (
              <section key={i}>
                <h2 className="mb-2 text-sm font-semibold text-fg">{section.heading}</h2>
                <div className="space-y-2 text-sm leading-relaxed text-fg">
                  {section.body.map((block, j) =>
                    typeof block === "string" ? (
                      <p key={j}>{block}</p>
                    ) : (
                      <ul key={j} className="list-disc space-y-1 pl-5">
                        {block.list.map((item, k) => (
                          <li key={k}>{item}</li>
                        ))}
                      </ul>
                    )
                  )}
                </div>
              </section>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
