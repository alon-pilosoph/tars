/* The screenshots in docs/screenshots/, of the current build with its demo data. `npm run screenshots`. */
import { test } from "@playwright/test";
import path from "node:path";
import { ORIGIN, REPO, serveFromDisk } from "./serve";

const PAGES = ["home", "sent", "review", "voices", "models"];
const SIZES = { desktop: { width: 1280, height: 900 }, phone: { width: 390, height: 844 } };

for (const tab of PAGES)
  for (const [size, viewport] of Object.entries(SIZES))
    for (const theme of ["light", "dark"])
      test(`${tab}, ${size} ${theme}`, async ({ page }) => {
        await serveFromDisk(page);
        await page.setViewportSize(viewport);
        await page.goto(`${ORIGIN}/index.html?demo&tab=${tab}&theme=${theme}`);
        await page.locator("main h1").first().waitFor();
        await page.evaluate(() => document.fonts.ready.then(() => undefined));
        await page.screenshot({
          path: path.join(REPO, "docs/screenshots", `${tab}-${size}-${theme}.png`),
          animations: "disabled",
        });
      });
