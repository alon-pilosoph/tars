/* The screenshot tests and the docs screenshots load the build straight from disk, with a fixed clock. */
import type { Page } from "@playwright/test";
import path from "node:path";

export const REPO = path.resolve(import.meta.dirname, "../..");
export const ORIGIN = "http://pixel.test";
export const SIZES = { desktop: { width: 1280, height: 900 }, phone: { width: 390, height: 844 } };
const NOW = new Date("2026-09-26T10:00:00"); // demo times are relative to now
const BUILD = path.join(REPO, "src/voice_assistant/webui_static");

export async function serveFromDisk(page: Page) {
  await page.clock.setFixedTime(NOW);
  await page.route(`${ORIGIN}/**`, async route => {
    const p = new URL(route.request().url()).pathname;
    const file =
      p === "/index.html" ? path.join(BUILD, "index.html") : p.startsWith("/assets/") ? path.join(BUILD, p) : null;
    return file ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
}

export const fontsReady = (page: Page) => page.evaluate(() => document.fonts.ready.then(() => undefined));
