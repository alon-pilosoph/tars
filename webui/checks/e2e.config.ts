import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

// A throwaway copy of the demo log: the checks rename, move and delete things.
const DATA = mkdtempSync(path.join(tmpdir(), "tars-e2e-"));
const PORT = 8094;

export default defineConfig({
  testDir: ".",
  testMatch: "e2e.spec.ts",
  workers: 1,
  reporter: [["list"]],
  timeout: 120_000,
  // The installed Chrome locally; Playwright's own Chromium in CI (CI=true), which also runs on Linux on ARM.
  use: { channel: process.env.CI ? undefined : "chrome", headless: true, baseURL: `http://127.0.0.1:${PORT}` },
  webServer: {
    command: `uv run python tools/webui_demo.py build ${DATA} && uv run python tools/webui_demo.py serve ${DATA} ${PORT}`,
    cwd: path.resolve(import.meta.dirname, "../.."),
    url: `http://127.0.0.1:${PORT}/api/status`,
    timeout: 180_000,
    reuseExistingServer: false,
  },
});
