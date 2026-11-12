import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import path from "node:path";

// A fresh copy of the demo log each run, since the checks rename, move and delete things. The folder is fixed
// because Playwright loads this file in every process, and a new folder per load would leave copies behind.
const DATA = path.join(tmpdir(), "tars-e2e");
const PORT = 8094;
// Sign-in's own backend, with [web] sign_in on and an empty log: signin.spec.ts. Opened as localhost, the one name
// browsers make passkeys for over plain HTTP.
export const SIGN_IN_DATA = path.join(tmpdir(), "tars-e2e-sign-in");
const SIGN_IN_PORT = 8095;
export const SIGN_IN_URL = `http://localhost:${SIGN_IN_PORT}`;

export default defineConfig({
  testDir: ".",
  testMatch: ["e2e.spec.ts", "signin.spec.ts"],
  workers: 1,
  reporter: [["list"]],
  timeout: 120_000,
  // Installed Chrome locally; in CI, Playwright's own Chromium, which also runs on Linux on ARM.
  use: { channel: process.env.CI ? undefined : "chrome", headless: true, baseURL: `http://127.0.0.1:${PORT}` },
  webServer: [
    {
      command:
        `rm -rf '${DATA}' && uv run python tools/webui_demo.py build '${DATA}' ` +
        `&& uv run python tools/webui_demo.py serve '${DATA}' ${PORT}`,
      cwd: path.resolve(import.meta.dirname, "../.."),
      url: `http://127.0.0.1:${PORT}/api/status`,
      timeout: 180_000,
      reuseExistingServer: false,
    },
    {
      command: `rm -rf '${SIGN_IN_DATA}' && uv run python tools/webui_demo.py serve '${SIGN_IN_DATA}' ${SIGN_IN_PORT} --sign-in`,
      cwd: path.resolve(import.meta.dirname, "../.."),
      url: `http://127.0.0.1:${SIGN_IN_PORT}/sign-in`,
      timeout: 180_000,
      reuseExistingServer: false,
    },
  ],
});
