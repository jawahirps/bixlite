// BIX brand lock-up from bix.ghafservices.com: cube mark + "Bıx" wordmark
// (dotless ı with a teal orb), followed by the "Lite" edition name.
export default function BixLogo({ size = "md", caption }) {
  const big = size === "lg";
  return (
    <span className={`inline-flex items-center ${big ? "gap-3" : "gap-2"}`}>
      <img
        src="/bix-cube.svg"
        alt=""
        className={big ? "h-12 w-12" : "h-9 w-9"}
        style={{ filter: "drop-shadow(0 3px 5px rgba(11,34,51,.28))" }}
      />
      <span className="flex flex-col items-start leading-none">
        <span className={`gh-product ${big ? "text-[34px]" : "text-[24px]"}`} aria-label="Bix Lite">
          B
          <span className="relative">
            ı
            <span className="absolute left-1/2 top-[0.08em] h-[0.19em] w-[0.19em] -translate-x-1/2 rounded-full bg-ghaf-600" />
          </span>
          x<span className="ml-[0.2em] font-semibold text-ghaf-600">Lite</span>
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
