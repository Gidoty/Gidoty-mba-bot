// Every chart has this: the same numbers as a table, for screen readers,
// for anyone who prefers figures, and for exact values the bars cannot show.
// `columns` are header strings; `rows` are arrays of cells (text or elements).
export default function ChartFallback({ summary = "View as table", columns, rows }) {
  return (
    <details className="text-sm">
      <summary className="w-fit cursor-pointer font-semibold text-brand-text">{summary}</summary>
      <div className="mt-2 max-h-60 overflow-auto">
        <table className="w-full text-sm">
          <thead>
            <tr>
              {columns.map((c, i) => (
                <th
                  key={c}
                  className={`border-b border-line px-2 py-1.5 text-xs font-semibold uppercase tracking-wide text-muted ${
                    i ? "text-right" : "text-left"
                  }`}
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="px-2 py-2 text-muted">
                  No data in this range.
                </td>
              </tr>
            ) : (
              rows.map((row, r) => (
                <tr key={r} className="border-b border-line last:border-0">
                  {row.map((cell, i) => (
                    <td key={i} className={`whitespace-nowrap px-2 py-1.5 font-mono ${i ? "text-right" : "text-left font-sans"}`}>
                      {cell}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </details>
  );
}
