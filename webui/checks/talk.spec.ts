/* The Talk page in a real browser: a tap opens the microphone, the page streams it to the brain over the body link,
   plays the reply and says it played; the brain's words show as they come. Chromium's fake microphone plays a tone. */
import { expect, test } from "@playwright/test";

test("a tap, a question streamed, the reply played to its end, and back to waiting", async ({ page }) => {
  await page.goto("/?tab=talk");
  const button = page.locator(".talk-button");
  await expect(page.locator(".talk-state")).toHaveText("Tap to talk");
  await expect(page.getByRole("link", { name: "Talk" }).first()).toHaveAttribute("aria-current", "page");
  await button.click();
  await expect(page.locator(".talk-state")).toHaveText("Listening…");
  const heard = page.locator(".talk-line.you");
  await expect(heard).toContainText("ms of sound", { timeout: 15_000 });
  const [, ms, loudest] = (await heard.textContent())!.match(/(\d+) ms of sound, loudest (\d+)/)!;
  expect(Number(ms)).toBeGreaterThanOrEqual(1200); // it streamed the whole second and a half, at 16 kHz
  expect(Number(loudest)).toBeGreaterThan(1000); // and it was the microphone's tone, not silence
  await expect(page.locator(".talk-line.tars")).toHaveText(/Played it all\./, { timeout: 15_000 });
  await expect(page.locator(".talk-state")).toHaveText("Tap to talk");
});
