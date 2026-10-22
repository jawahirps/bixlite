/** @type {import('tailwindcss').Config} */
// Ghaf design system — https://claude.ai/artifact/BKAGZyt7HzxU99c1hK8k2L
// `ghaf` is the brand teal scale; `slate` is remapped to Ghaf's navy/sage neutrals
// so existing pages pick up the brand without per-class edits.
const here = new URL(".", import.meta.url).pathname;

export default {
  content: [`${here}index.html`, `${here}src/**/*.{js,jsx}`],
  theme: {
    extend: {
      colors: {
        ghaf: {
          50: "#f1f7f9",
          100: "#e0eef2",
          200: "#bcdce5",
          300: "#86c0cf",
          400: "#3f97ad",
          500: "#0f7c97",
          600: "#006884",
          700: "#00576e",
          800: "#0b4458",
          900: "#0b2c3c",
        },
        slate: {
          50: "#f6f8f3",
          100: "#eef2e9",
          200: "#e5eee6",
          300: "#cad5ca",
          400: "#7f9691",
          500: "#596d66",
          600: "#42606c",
          700: "#36555b",
          800: "#0b2c3c",
          900: "#0b2233",
          950: "#071826",
        },
        ink: { DEFAULT: "#0b2c3c", deep: "#0b2233" },
        sage: { DEFAULT: "#eef2e9", mint: "#e5eee6", line: "#b7cac6" },
        paid: "#2e7d5b",
        denied: "#b3412e",
        pending: "#8a5d0b",
      },
      fontFamily: {
        sans: ['"DM Sans"', "Arial", "sans-serif"],
        display: ["Manrope", '"DM Sans"', "Arial", "sans-serif"],
        product: ["Geist", "Manrope", "sans-serif"],
      },
      borderColor: { hairline: "rgba(0,104,132,0.16)" },
    },
  },
  plugins: [],
};
