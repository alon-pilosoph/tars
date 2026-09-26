import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "screenshots.spec.ts",
  workers: 2,
  reporter: [["list"]],
  use: { channel: "chrome", headless: true },
});
