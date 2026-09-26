/* Screenshot tests: every state a link can open, and the page after each interaction, on desktop and phone (and
   the main pages in dark), against the approved baselines in checks/screenshots/. `npm run visual`; after an
   intended change, `npm run visual:update` and look at what changed before committing the new images. */
import { expect, test } from "@playwright/test";
import { ORIGIN, serveFromDisk } from "./serve";

const STATES = [
  "",
  "seen=all",
  "person=2",
  "demo=empty",
  "state=loading",
  "state=error",
  "seen=all&conv=11",
  "conv=2",
  "conv=4",
  "conv=5",
  "conv=6",
  "conv=7",
  "conv=9",
  "conv=3&edit=31",
  "conv=10",
  "conv=2&playing=turn-23",
  "conv=2&modal=delete-conv:2",
  "tab=sent",
  "tab=sent&modal=note:101",
  "tab=sent&item=103",
  "tab=sent&item=107",
  "tab=sent&item=108",
  "tab=sent&modal=delete-item:103",
  "tab=sent&person=2",
  "tab=review",
  "tab=review&review=done",
  "tab=review&modal=delete",
  "more=1",
  "tab=voices",
  "tab=voices&modal=merge",
  "tab=voices&toast=recluster",
  "tab=models&retrain=better",
  "tab=models&modal=rollback",
  "tab=sent&person=household",
  "tab=sent&demo=empty",
  "tab=review&demo=empty",
  "tab=review&playing=13-wake",
  "tab=review&modal=newvoice",
  "tab=voices&playing=1-request",
  "tab=voices&recluster=running",
  "tab=voices&toast=renamed",
  "tab=voices&demo=empty",
  "tab=models",
  "tab=models&retrain=running",
  "tab=models&retrain=skipped",
  "tab=models&retrain=worse",
  "tab=models&demo=empty",
];

/** A step both pages take: click the i-th match, click the first whose text starts with…, or type into a field. */
type Step = { click: string; i?: number } | { clickText: string; text: string } | { fill: string; value: string };
const ACTS: [string, string, Step[]][] = [
  ["item menu", "", [{ click: ".it-h .more" }]],
  ["conversation menu", "", [{ click: ".conv-h .more", i: 1 }]],
  ["expand a long conversation", "", [{ click: ".more-turns" }]],
  ["rate a reply good", "", [{ click: ".rate .g" }]],
  ["rate it again to clear", "conv=9", [{ click: ".rate .g" }]],
  ["start a fix", "", [{ clickText: ".tact", text: "Fix text" }]],
  ["cancel a fix", "conv=3&edit=31", [{ clickText: ".edit .btn", text: "Cancel" }]],
  [
    "save a fix",
    "conv=3&edit=31",
    [
      { fill: ".edit textarea", value: "Add eggs, milk and coffee to the shopping list." },
      { clickText: ".edit .btn", text: "Save" },
    ],
  ],
  ["tick a list entry", "", [{ click: ".entries input", i: 3 }]],
  ["show the rest of a list", "tab=sent", [{ clickText: ".it-a .btn", text: "3 more" }]],
  ["open a note", "", [{ clickText: ".it-a .btn", text: "Open" }]],
  ["filter by Stacey", "", [{ click: ".aside-r .seg button", i: 2 }]],
  ["nudge to Review", "", [{ click: ".nudge" }]],
  ["nav to Sent", "", [{ clickText: ".nav button", text: "Sent" }]],
  ["Sent: household", "tab=sent", [{ clickText: ".aside-r .seg button", text: "Household" }]],
  ["Sent: from the conversation", "tab=sent", [{ click: ".it-f .tact", i: 2 }]],
  ["More menu (phone)", "", [{ click: ".moreb" }]],
  ["Review: label yes", "tab=review", [{ click: "article .lab-seg .y" }]],
  ["Review: event menu", "tab=review", [{ click: "article .more" }]],
  ["Review: voice chip", "tab=review", [{ click: ".chip" }]],
  ["Voices: card menu", "tab=voices", [{ click: ".voice .more", i: 2 }]],
  ["Voices: name dialog", "tab=voices", [{ click: ".vfoot .btn" }]],
  ["Name this voice", "", [{ clickText: ".tact", text: "Name this voice" }]],
];
const SIZES = { desktop: { width: 1280, height: 900 }, phone: { width: 390, height: 844 } };

function act(steps: Step[]) {
  for (const s of steps) {
    if ("click" in s) (document.querySelectorAll(s.click)[s.i ?? 0] as HTMLElement).click();
    else if ("clickText" in s)
      (
        [...document.querySelectorAll(s.clickText)].find(el => el.textContent!.trim().startsWith(s.text)) as HTMLElement
      ).click();
    else (document.querySelector(s.fill) as HTMLTextAreaElement).value = s.value;
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

// Dark mode swaps the colour tokens only, so it's checked on each page and on the overlays, not on every state.
const DARK = new Set([
  "home",
  "tab-sent",
  "tab-review",
  "tab-voices",
  "tab-models",
  "demo-empty",
  "state-error",
  "tab-sent-modal-note-101",
  "conv-2-modal-delete-conv-2",
  "act-item-menu",
  "act-voices-name-dialog",
]);

for (const [name, q, steps] of jobs)
  for (const theme of DARK.has(name) ? ["light", "dark"] : ["light"])
    for (const [size, viewport] of Object.entries(SIZES))
      test(`${name}, ${size} ${theme}`, async ({ page }) => {
        await page.setViewportSize(viewport);
        const query = [q.includes("demo=") ? "" : "demo", q, `theme=${theme}`].filter(Boolean).join("&");
        await page.goto(`${ORIGIN}/index.html?${query}`);
        await page.evaluate(() => document.fonts.ready.then(() => undefined));
        if (steps) {
          await expect(page.locator("main h1").first()).toBeVisible();
          await page.evaluate(act, [...steps]);
        }
        await expect(page).toHaveScreenshot(`${name}--${size}-${theme}.png`);
      });
