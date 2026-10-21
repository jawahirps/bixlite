export default function DataTable({ columns, rows, emptyLabel }) {
  if (!rows || rows.length === 0) {
    return <p className="py-6 text-center text-sm text-slate-600">{emptyLabel || "No data available."}</p>;
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-hairline">
      <table className="min-w-full text-[13px]">
        <thead>
          <tr className="bg-sage-mint">
            {columns.map((col) => (
              <th
                key={col.key}
                className="whitespace-nowrap px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-600"
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[rgba(0,104,132,0.12)]">
          {rows.map((row, idx) => (
            <tr key={idx} className="hover:bg-sage">
              {columns.map((col) => (
                <td key={col.key} className="whitespace-nowrap px-3 py-3 text-ink tabular-nums">
                  {col.render ? col.render(row[col.key], row) : row[col.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
