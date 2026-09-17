export function ResultTable({ columns, rows }: { columns: string[]; rows: unknown[][] }) {
  if (!columns.length || !rows.length) return null
  const shown = rows.slice(0, 50)

  return (
    <div className="overflow-auto rounded-lg border border-(--color-edge)">
      <table className="w-full text-sm">
        <thead className="bg-(--color-panel) text-left text-(--color-muted)">
          <tr>
            {columns.map((column) => (
              <th key={column} className="px-3 py-2 font-medium whitespace-nowrap">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, rowIndex) => (
            <tr key={rowIndex} className="border-t border-(--color-edge)">
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="px-3 py-1.5 whitespace-nowrap text-slate-200">
                  {cell === null || cell === undefined ? (
                    <span className="text-(--color-muted)">null</span>
                  ) : (
                    String(cell)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && (
        <div className="px-3 py-2 text-xs text-(--color-muted)">
          showing {shown.length} of {rows.length} rows
        </div>
      )}
    </div>
  )
}
