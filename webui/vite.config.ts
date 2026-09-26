import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The build is committed and served by the Python package (no Node on the Pi).
// `npm run dev` proxies the API to a running `voice-assistant --web` (or tools/webui_demo.py) on :8080, or $TARS_API.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/voice_assistant/webui_static", emptyOutDir: true, assetsInlineLimit: 0 },
  server: { proxy: { "/api": process.env.TARS_API ?? "http://127.0.0.1:8080" } },
  test: { include: ["src/**/*.test.ts"], setupFiles: ["src/test-setup.ts"] },
});
