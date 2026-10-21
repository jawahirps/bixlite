import { fileURLToPath } from "node:url";

export default {
  plugins: {
    // Explicit path so the config loads even when Vite is started from another directory.
    tailwindcss: { config: fileURLToPath(new URL("./tailwind.config.js", import.meta.url)) },
    autoprefixer: {},
  },
};
