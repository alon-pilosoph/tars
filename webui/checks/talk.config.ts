import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import path from "node:path";

// The Talk page against the real backend and a scripted brain on the real body link (tools/webui_demo.py --talk),
// with Chromium's fake microphone, which plays a steady tone. `npm run e2e` runs it with the other checks.
const DATA = path.join(tmpdir(), "tars-e2e-talk");
const PORT = 8096;
const TALK_PORT = 8097;

export default defineConfig({
  testDir: ".",
  testMatch: ["talk.spec.ts"],
  workers: 1,
  reporter: [["list"]],
  timeout: 60_000,
  use: {
    channel: process.env.CI ? undefined : "chrome",
    headless: true,
    baseURL: `http://localhost:${PORT}`, // localhost: a secure page without HTTPS, so the microphone may open
    permissions: ["microphone"],
    launchOptions: { args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] },
  },
  webServer: {
    command:
      `rm -rf '${DATA}' && uv run python tools/webui_demo.py build '${DATA}' ` +
      `&& uv run python tools/webui_demo.py serve '${DATA}' ${PORT} --talk ${TALK_PORT}`,
    cwd: path.resolve(import.meta.dirname, "../.."),
    url: `http://127.0.0.1:${PORT}/api/status`,
    timeout: 180_000,
    reuseExistingServer: false,
  },
});
