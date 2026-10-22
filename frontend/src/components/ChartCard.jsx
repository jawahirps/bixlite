export default function ChartCard({ title, description, children, className }) {
  return (
    <div className={`gh-section ${className || ""}`}>
      <h3 className="gh-section-title">{title}</h3>
      {description && <p className="mt-1 text-xs text-slate-600">{description}</p>}
      <div className="mt-4">{children}</div>
    </div>
  );
}
