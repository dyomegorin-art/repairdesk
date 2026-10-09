import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the API runs on :8000; the compiled PWA is served by the backend itself.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000", "/health": "http://localhost:8000" } },
});
