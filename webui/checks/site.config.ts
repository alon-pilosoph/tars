import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "site.spec.ts",
  workers: 1,
  reporter: [["list"]],
  use: { channel: process.env.CI ? undefined : "chrome", headless: true },
});
