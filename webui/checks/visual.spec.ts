/* Screenshot tests: every state a demo link can open, and the page after each interaction, on desktop and phone,
   against the approved baselines in checks/screenshots/. `npm run visual`; after an
   intended change, `npm run visual:update` and look at what changed before committing the new images. */
import { expect, test } from "@playwright/test";
import { ORIGIN, SIZES, pageReady, serveFromDisk, settled } from "./serve";

const STATES = [
  "",
  "seen=all",
  "person=2",
  "demo=empty",
  "demo=long",
  "state=loading",
  "state=error",
  "more=1",
  "conv=1",
  "conv=2",
  "conv=4",
  "conv=5",
  "conv=6",
  "conv=7",
  "conv=9",
  "conv=10",
  "conv=12",
  "edit=31",
  "conv=1&playing=turn-11",
  "menu=who:1",
  "conv=1&modal=delete-conv:1",
  "tab=sent",
  "tab=sent&modal=note:101",
  "tab=sent&item=106",
  "tab=sent&item=107",
  "tab=sent&item=108",
  "tab=sent&menu=item:104",
  "tab=sent&modal=delete-item:104",
  "tab=sent&person=1",
  "tab=sent&person=household",
  "tab=sent&demo=empty",
  "tab=review",
  "tab=review&answered=18:real",
  "tab=review&answered=18:real&refreshed=1",
  "tab=review&review=done",
  "tab=review&playing=20-wake",
  "tab=review&menu=wake:15",
  "tab=review&menu=reply:14",
  "tab=review&modal=newvoice:14",
  "tab=review&modal=delete-wake:15",
  "tab=review&demo=empty",
  "tab=reminders",
  "tab=reminders&form=time",
  "tab=reminders&form=back",
  "tab=reminders&form=minutes",
  "tab=reminders&form=error",
  "tab=reminders&modal=stop:9",
  "tab=reminders&reminders=off",
  "tab=reminders&demo=empty",
  "tab=voices",
  "tab=voices&menu=voice:3",
  "tab=voices&modal=newvoice:3",
  "tab=voices&modal=merge:3",
  "tab=voices&recluster=running",
  "tab=voices&toast=recluster",
  "tab=voices&toast=renamed",
  "tab=voices&playing=19-request",
  "tab=voices&demo=empty",
  "tab=models",
  "tab=models&modal=rollback:v1",
  "tab=models&demo=empty",
];

/** Click the i-th match, click the first match whose text starts with `text`, or type into a field. */
type Step = { click: string; i?: number } | { clickText: string; text: string } | { fill: string; value: string };
const ACTS: [string, string, Step[]][] = [
  ["open a conversation", "", [{ click: ".conv-body" }]],
  ["close a conversation", "conv=1", [{ click: ".panel-head .icon-btn" }]],
  ["expand a long conversation", "", [{ click: ".more-turns" }]],
  ["rate a reply good", "", [{ click: ".rate-good" }]],
  ["rate it again to clear", "conv=9", [{ click: '.conv[data-id="9"] .rate-good' }]],
  ["start a fix", "conv=1", [{ clickText: ".p-row .btn", text: "Fix text" }]],
  ["cancel a fix", "edit=31", [{ clickText: ".edit .btn", text: "Cancel" }]],
  [
    "save a fix",
    "edit=31",
    [
      { fill: ".edit textarea", value: "Add eggs, milk and coffee to the shopping list." },
      { clickText: ".edit .btn", text: "Save" },
    ],
  ],
  ["who was talking", "conv=1", [{ clickText: ".p-actions .btn", text: "Who was talking" }]],
  ["item menu", "tab=sent", [{ click: ".item [aria-haspopup=menu]" }]],
  ["tick a list entry", "tab=sent", [{ click: ".entry", i: 3 }]],
  ["show the rest of a list", "tab=sent", [{ click: ".item .btns button[aria-expanded]" }]],
  ["open a note", "tab=sent", [{ clickText: ".item .btns .btn", text: "Open" }]],
  ["filter by Stacey", "", [{ click: ".seg button", i: 2 }]],
  ["nudge to Review", "", [{ click: ".nudge a" }]],
  ["a new item, from Home", "", [{ click: ".rows .row" }]],
  ["nav to Sent", "", [{ clickText: ".nav a", text: "Sent" }]],
  ["Sent: household", "tab=sent", [{ clickText: ".seg button", text: "Household" }]],
  ["Sent: from the conversation", "tab=sent", [{ click: ".from-conv a", i: 2 }]],
  ["More menu (phone)", "", [{ click: ".more-btn" }]],
  ["Review: label yes", "tab=review", [{ click: ".answer-yes" }]],
  ["Review: event menu", "tab=review", [{ click: ".wake-head [aria-haspopup=menu]" }]],
  ["Review: voice chip", "tab=review", [{ click: ".chip" }]],
  ["Voices: card menu", "tab=voices", [{ click: ".voice [aria-haspopup=menu]", i: 2 }]],
  ["Voices: name dialog", "tab=voices", [{ clickText: ".voice .btn", text: "Name it" }]],
  ["Name this voice", "", [{ click: ".name-link" }]],
  ["Reminders: new", "tab=reminders", [{ clickText: ".page-head .btn", text: "New reminder" }]],
  ["Reminders: stop", "tab=reminders", [{ clickText: ".rem .btn", text: "Stop" }]],
];

function act(steps: Step[]) {
  const at = <E extends Element>(el: E | undefined, what: string) => {
    if (!el) throw new Error(`nothing matches ${what}`);
    el.scrollIntoView({ block: "center" });
    return el;
  };
  for (const s of steps) {
    if ("click" in s) {
      at(document.querySelectorAll<HTMLElement>(s.click)[s.i ?? 0], s.click).click();
    } else if ("clickText" in s) {
      const all = [...document.querySelectorAll<HTMLElement>(s.clickText)];
      at(
        all.find(el => el.textContent?.trim().startsWith(s.text)),
        `${s.clickText} “${s.text}”`,
      ).click();
    } else {
      at(document.querySelector<HTMLTextAreaElement>(s.fill) ?? undefined, s.fill).value = s.value;
    }
  }
}

const slug = (s: string) =>
  s
    .replace(/[^a-z0-9]+/gi, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase() || "home";
const jobs = [
  ...STATES.map(q => [slug(q), q, null] as const),
  ...ACTS.map(([name, q, steps]) => [`act-${slug(name)}`, q, steps] as const),
];

test.beforeEach(({ page }) => serveFromDisk(page));

for (const [name, q, steps] of jobs)
  for (const [size, viewport] of Object.entries(SIZES))
    test(`${name}, ${size}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      const query = [q.includes("demo=") ? "" : "demo", q].filter(Boolean).join("&");
      await page.goto(`${ORIGIN}/index.html?${query}`);
      await pageReady(page);
      if (steps) {
        await page.evaluate(act, [...steps]);
        await settled(page);
      }
      await expect(page).toHaveScreenshot(`${name}--${size}.png`);
    });

test("the header fits at every width: the tab bar below on a phone, the tabs on top above it, and nothing leaves the screen", async ({
  page,
}) => {
  await serveFromDisk(page);
  for (const width of [360, 390, 480, 640, 720, 721, 860, 1040, 1041, 1280]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`${ORIGIN}/index.html?demo`);
    await pageReady(page);
    const phone = width <= 720;
    await expect(page.locator(".tabbar"), `at ${width}px`).toBeVisible({ visible: phone });
    await expect(page.locator(".top .nav"), `at ${width}px`).toBeVisible({ visible: !phone });
    if (!phone) {
      const nav = (await page.locator(".top .nav").boundingBox())!;
      const end = (await page.locator(".top .top-end").boundingBox())!;
      expect(nav.x + nav.width, `at ${width}px`).toBeLessThanOrEqual(end.x);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth), `at ${width}px`).toBe(width);
  }
});
