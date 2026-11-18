// BIX brand lock-up from bix.ghafservices.com: cube mark + "Bıx" wordmark
// (dotless ı with a teal orb), followed by the "Lite" edition name.
// Animated: the cube's blocks float in a staggered loop, the orb pulses and a
// light shimmer sweeps across the wordmark. All motion stops for users who ask
// for reduced motion (see .bix-* rules in index.css).

// Cube blocks: [pivot x, pivot y, polygons [points, fill]...], drawn back to front
const BLOCKS = [
  [40, 43.3, [["40,56 29,49.65 29,36.95 40,43.3", "#00596F"], ["40,56 51,49.65 51,36.95 40,43.3", "#3D9CB2"]]],
  [51, 36.95, [["51,49.65 62,43.3 62,30.6 51,36.95", "#2E8A9E"]]],
  [29, 36.95, [["29,49.65 18,43.3 18,30.6 29,36.95", "#014356"]]],
  [40, 30.6, [["40,43.3 29,36.95 29,24.25 40,30.6", "#014356"], ["40,43.3 51,36.95 51,24.25 40,30.6", "#2E8A9E"], ["40,30.6 51,24.25 40,17.9 29,24.25", "#8CD3DF"]]],
  [51, 24.25, [["51,36.95 62,30.6 62,17.9 51,24.25", "#3D9CB2"], ["51,24.25 62,17.9 51,11.55 40,17.9", "#76C3D2"]]],
  [29, 24.25, [["29,36.95 18,30.6 18,17.9 29,24.25", "#00596F"], ["29,24.25 40,17.9 29,11.55 18,17.9", "#76C3D2"]]],
  [40, 17.9, [["40,17.9 51,11.55 40,5.2 29,11.55", "#8CD3DF"]]],
];

function BixCube({ className }) {
  return (
    <svg
      viewBox="10 1 60 60"
      className={className}
      style={{ filter: "drop-shadow(0 3px 5px rgba(11,34,51,.28))", overflow: "visible" }}
      aria-hidden="true"
    >
      {BLOCKS.map(([x, y, polys], i) => (
        // outer <g> carries the CSS animation; inner keeps the original resting transform
        <g key={i} className="bix-block" style={{ animationDelay: `${i * 0.16}s` }}>
          <g transform={`translate(${x},${y}) scale(.955) translate(${-x},${-y})`}>
            {polys.map(([pts, fill]) => (
              <polygon key={pts} points={pts} fill={fill} />
            ))}
          </g>
        </g>
      ))}
    </svg>
  );
}

export default function BixLogo({ size = "md", caption }) {
  const big = size === "lg";
  return (
    <span className={`inline-flex items-center ${big ? "gap-3" : "gap-2"}`}>
      <BixCube className={big ? "h-12 w-12" : "h-9 w-9"} />
      <span className="flex flex-col items-start leading-none">
        <span className={`gh-product ${big ? "text-[34px]" : "text-[24px]"}`} aria-label="Bix Lite">
          <span className="bix-shimmer" aria-hidden="true">
            B
            <span className="relative">
              ı
              <span className="bix-orb absolute left-1/2 top-[0.08em] h-[0.19em] w-[0.19em] -translate-x-1/2 rounded-full bg-ghaf-600" />
            </span>
            x
          </span>
          <span className="bix-shimmer bix-shimmer-teal ml-[0.2em] font-semibold" aria-hidden="true">
            Lite
          </span>
        </span>
        {caption && (
          <span className="mt-1 text-[9px] font-semibold uppercase tracking-[0.16em] text-slate-600">
            {caption}
          </span>
        )}
      </span>
    </span>
  );
}
