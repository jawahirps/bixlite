import { useState } from "react";

// Lightweight stacked bar chart (replaces recharts on the Dashboard, which cost ~386 KB).
// data: [{ label, values: { [key]: number } }], series: [{ key, color }]
export default function StackedBars({ data, series, height = 320, format = (v) => v, tooltip }) {
  const [hover, setHover] = useState(null);
  const totals = data.map((d) => series.reduce((s, x) => s + Number(d.values[x.key] || 0), 0));
  const max = niceMax(Math.max(0, ...totals));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => max * f);

  return (
    <div>
      <div className="relative flex" style={{ height }}>
        <div className="flex w-14 shrink-0 flex-col-reverse justify-between pb-6 pr-2 text-right text-xs text-slate-500 tabular-nums">
          {ticks.map((t) => <span key={t} className="leading-none">{format(t)}</span>)}
        </div>
        <div className="relative flex-1">
          <div className="absolute inset-x-0 top-0 bottom-6 flex flex-col justify-between">
            {ticks.map((t) => <div key={t} className="border-t border-dashed border-[#e5eee6]" />)}
          </div>
          <div className="absolute inset-x-0 top-0 bottom-6 flex items-end gap-[2%] px-[1%]">
            {data.map((d, i) => (
              <div key={d.label} className="relative flex h-full flex-1 flex-col-reverse"
                   onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}
                   style={{ background: hover === i ? "#f1f7f9" : undefined }}>
                {series.map((s, si) => {
                  const v = Number(d.values[s.key] || 0);
                  const last = si === series.length - 1;
                  return <div key={s.key} style={{ height: `${max ? (v / max) * 100 : 0}%`, background: s.color,
                    borderRadius: last ? "4px 4px 0 0" : 0 }} />;
                })}
                {hover === i && tooltip && (
                  <div className={`pointer-events-none absolute bottom-full z-10 mb-2 ${i > data.length / 2 ? "right-0" : "left-0"}`}>
                    {tooltip(d)}
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="absolute inset-x-0 bottom-0 flex h-6 items-end gap-[2%] px-[1%]">
            {data.map((d) => <span key={d.label} className="flex-1 whitespace-nowrap text-center text-[11px] text-slate-500">{d.label}</span>)}
          </div>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap justify-center gap-4 text-xs text-slate-700">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5" style={{ background: s.color }} />{s.key}
          </span>
        ))}
      </div>
    </div>
  );
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 4 ? 4 : n <= 5 ? 5 : 10) * p;
}
