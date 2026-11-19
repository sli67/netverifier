import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    // The browser only ever talks to this origin, so the session cookie stays
    // same-origin and the API needs no CORS configuration.
    proxy: {
      "/api": {
        target: process.env.API_URL ?? "http://127.0.0.1:3000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
