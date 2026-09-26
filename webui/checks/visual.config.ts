import { defineConfig } from "@playwright/test";

// Baselines are per platform: fonts render differently on macOS and Linux, so each needs its own.
export default defineConfig({
  testDir: ".",
  testMatch: "visual.spec.ts",
  snapshotPathTemplate: "{testDir}/screenshots/{platform}/{arg}{ext}",
  workers: 2,
  reporter: [["list"]],
  use: { channel: "chrome", headless: true },
  expect: { toHaveScreenshot: { animations: "disabled", caret: "hide" } },
});
