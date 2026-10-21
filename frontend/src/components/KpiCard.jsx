export default function KpiCard({ label, value, accent, color, hint }) {
  return (
    <div className="gh-card p-5">
      <p className="gh-eyebrow">{label}</p>
      <p
        className={`mt-2 font-display text-[28px] font-bold leading-9 tracking-[-0.03em] tabular-nums ${
          accent || color || "text-ink"
        }`}
      >
        {value}
      </p>
      {hint && <p className="mt-1 text-[13px] text-slate-600">{hint}</p>}
    </div>
  );
}
