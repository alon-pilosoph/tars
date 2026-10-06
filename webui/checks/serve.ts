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

/** Loaded, scrolled to what the link points at, any linked state open, and the fonts in: what main.tsx marks. */
export async function pageReady(page: Page) {
  await page.waitForFunction(() => document.documentElement.dataset.ready === "true");
  await fontsReady(page);
}

/** Everything a click set off has landed (main.tsx's tarsSettled), and the page has drawn it. */
export async function settled(page: Page) {
  await page.evaluate(async () => {
    await (window as unknown as { tarsSettled: () => Promise<void> }).tarsSettled();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  });
}
