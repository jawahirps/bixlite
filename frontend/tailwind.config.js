/** @type {import('tailwindcss').Config} */
// Ghaf design system — https://claude.ai/artifact/BKAGZyt7HzxU99c1hK8k2L
// `ghaf` is the brand teal scale; `slate` is remapped to Ghaf's navy/sage neutrals
// so existing pages pick up the brand without per-class edits.
const here = new URL(".", import.meta.url).pathname;

const config = {
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
        // Status scales: the stock red / green / amber families are remapped to the
        // brand's denied / paid / pending tones, so every page's status colours match.
        red: {
          50: "#fbf1ef", 100: "#f6dfda", 200: "#ecbcb2", 300: "#dd9282", 400: "#c9644f",
          500: "#b3412e", 600: "#a03a29", 700: "#8a3222", 800: "#6f281c", 900: "#561f16",
        },
        emerald: {
          50: "#eff7f3", 100: "#d9ede3", 200: "#b3dbc8", 300: "#82c2a5", 400: "#4f9f7d",
          500: "#3a8a68", 600: "#2e7d5b", 700: "#26694c", 800: "#1f553e", 900: "#184431",
        },
        amber: {
          50: "#fbf6ea", 100: "#f5e9cc", 200: "#ebd39b", 300: "#ddb766", 400: "#c99a3a",
          500: "#a8781a", 600: "#8a5d0b", 700: "#744e09", 800: "#5e3f07", 900: "#4a3206",
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

const c = config.theme.extend.colors;
Object.assign(c, {
  green: c.emerald, rose: c.red, yellow: c.amber, orange: c.amber,
  sky: c.ghaf, teal: c.ghaf, blue: c.ghaf,
});

export default config;
