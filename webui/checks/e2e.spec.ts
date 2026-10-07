/* Functional check of the web UI against the real backend and a throwaway copy of the demo log. `npm run e2e`.
   The steps run in order in one page and depend on each other. */
import { type APIRequestContext, type Locator, type Page, expect, test } from "@playwright/test";
import type { ApiConversation, Cluster, Item, ModelsInfo, RemindersInfo, TarsEvent } from "../src/types";

const api = (r: APIRequestContext) => {
  const json = async <T>(path: string) => (await (await r.get(path)).json()) as T;
  return {
    items: () => json<Item[]>("/api/items"),
    events: () => json<TarsEvent[]>("/api/events"),
    clusters: () => json<Cluster[]>("/api/clusters"),
    convs: () => json<ApiConversation[]>("/api/conversations"),
    conv: (id: number) => json<ApiConversation>(`/api/conversations/${id}`),
    models: () => json<ModelsInfo>("/api/models"),
    reminders: () => json<RemindersInfo>("/api/reminders"),
  };
};
const reviewTodo = (events: TarsEvent[]) => events.filter(e => e.follow !== "asked" && !e.label).length;

test.describe.configure({ mode: "serial" });
let page: Page;
let A: ReturnType<typeof api>;
const conv = (id: number) => page.locator(`.conv[data-id="${id}"]`);
const item = (id: number) => page.locator(`.item[data-id="${id}"]`);
const turn = (id: number) => page.locator(`[data-turn="${id}"]`);
const toast = () => page.locator(".toast .msg");
const undo = () => page.getByRole("status").getByRole("button", { name: "Undo" });
const nav = (name: string) => page.locator("header.top").getByRole("link", { name: new RegExp(`^${name}`) });
const menu = () => page.getByRole("menu");
const menuItem = (name: string | RegExp) =>
  menu().getByRole("menuitem", { name }).or(menu().getByRole("menuitemradio", { name })).first();
const dialog = () => page.getByRole("dialog");
const filter = (name: string) => page.getByRole("group", { name: "Whose" }).getByRole("button", { name });
const toCheck = () => page.locator("main > .wakes > .wake");
const reviewed = () => page.locator(".reviewed .wake");
const answered = '.ans[aria-pressed="true"]';
async function openMenu(button: Locator) {
  await button.click();
  await expect(menu()).toBeVisible();
}
async function open(id: number) {
  if (await conv(id).locator(".panel").count()) return;
  await conv(id)
    .locator(".conv-body .said")
    .first()
    .click({ position: { x: 4, y: 8 } });
  await expect(conv(id).locator(".panel")).toBeVisible();
}
async function refresh() {
  const button = page.getByRole("button", { name: "Refresh" }).first();
  await button.click();
  await expect(button).toHaveAttribute("aria-busy", "false");
}

test.beforeAll(async ({ browser }, info) => {
  page = await browser.newPage({ baseURL: info.project.use.baseURL });
  A = api(page.request);
  await page.goto("/");
  await expect(page.locator(".conv").first()).toBeVisible();
});
test.afterAll(() => page.close());

test("Home shows what's new first, and every count agrees with the server", async () => {
  const unseen = (await A.items()).filter(i => !i.seen);
  const n = reviewTodo(await A.events());
  const convs = await A.convs();
  await expect(page.getByRole("heading", { level: 2, name: "New from TARS" })).toBeVisible();
  await expect(page.locator(".rows .row")).toHaveCount(unseen.length);
  await expect(nav("Sent")).toHaveText(unseen.length ? `Sent ${unseen.length}` : "Sent");
  await expect(nav("Review")).toHaveText(n ? `Review ${n}` : "Review");
  await expect(page).toHaveTitle(`TARS (${unseen.length})`);
  await expect(page.locator(".conv")).toHaveCount(convs.length);
  await expect(page.locator(".nudge")).toContainText(`${n} wakes TARS wasn't sure about.`);
});

test("a long conversation folds its middle, and expands", async () => {
  const long = (await A.convs()).find(c => c.turns.length > 4 && !c.turns.some(t => t.not_for_tars));
  if (!long) throw new Error("the demo log needs a conversation with more than 4 turns and no asides");
  const fold = conv(long.id).getByRole("button", { name: /more turns$/ });
  await expect(fold).toHaveText(`${long.turns.length - 3} more turns`);
  await fold.click();
  await expect(conv(long.id).locator("[data-turn]")).toHaveCount(long.turns.length);
  await expect(fold).toHaveCount(0);
});

test("an opened answer says how long it took and who wrote it, and a failed one says where it failed", async () => {
  const convs = await A.convs();
  const timed = convs.find(c => c.turns.some(t => t.timings?.total != null && t.answered_by === "quick"));
  const failed = convs.find(c => c.turns.some(t => t.failed_at === "llm"));
  if (!timed || !failed) throw new Error("the demo log needs a timed answer and a failed one");
  const t = timed.turns.find(x => x.timings?.total != null && x.answered_by === "quick")!;
  await open(timed.id);
  await expect(turn(t.id).locator(".p-info")).toContainText(`Took ${t.timings!.total!.toFixed(1)} s`);
  await expect(turn(t.id).locator(".p-info")).toContainText("Answered on the Pi by the quick model.");
  const f = failed.turns.find(x => x.failed_at === "llm")!;
  await expect(turn(f.id).locator(".fail")).toHaveText("Failed while writing the answer.");
  await open(failed.id);
  await expect(turn(f.id).locator(".p-info")).toContainText(`The error was “${f.error}”.`);
});

test("Reminders: one waiting for a got it is acknowledged on the page; a new one is set; one is stopped", async () => {
  const card = (id: number) => page.locator(`[data-id="${id}"]:is(.needs, .rem)`);
  await nav("Reminders").click();
  const waiting = (await A.reminders()).reminders.find(r => r.status === "waiting" && r.kind !== "timer");
  if (!waiting) throw new Error("the demo log needs a message waiting for a got it");
  await expect(card(waiting.id).locator(".says")).toHaveText(`“${waiting.says}”`);
  await card(waiting.id).getByRole("button", { name: "Got it" }).click();
  await expect(card(waiting.id).locator(".rem-state")).toContainText("Got it on this page");
  const acked = (await A.reminders()).reminders.find(r => r.id === waiting.id)!;
  expect([acked.status, acked.acked_via]).toEqual(["acknowledged", "web"]);

  await page.getByRole("button", { name: "New reminder" }).click();
  const form = page.getByRole("form", { name: "New reminder" });
  await form.getByRole("button", { name: "Message" }).click();
  await form.getByLabel("What should TARS say?").fill("dinner's at eight");
  await form.locator("select").first().selectOption("__other");
  await form.getByLabel("Their name").fill("Bo");
  await form.locator("select").nth(1).selectOption("Stacey");
  await form.getByRole("button", { name: "When Bo is next heard" }).click();
  await expect(form.locator(".hint.warn")).toHaveText(/^TARS doesn't know Bo's voice yet/);
  await expect(form.getByRole("button", { name: "Set it" })).toBeDisabled();
  await form.locator("select").first().selectOption("Alon");
  await form.getByRole("button", { name: "Set it" }).click();
  await expect(toast()).toHaveText(/^Set\./);
  const made = (await A.reminders()).reminders.find(r => r.text === "dinner's at eight")!;
  expect([made.for_name, made.from_name, made.due, made.set_via]).toEqual(["Alon", "Stacey", null, "web"]);
  await expect(card(made.id).locator(".rem-when")).toHaveText("When Alon is next heard");

  await card(made.id).getByRole("button", { name: "Stop" }).click();
  await dialog().getByRole("button", { name: "Stop it" }).click();
  await expect.poll(async () => (await A.reminders()).reminders.find(r => r.id === made.id)?.status).toBe("cancelled");
  await nav("Home").click();
});

test("rating a reply saves it, and Undo clears it", async () => {
  const c = (await A.convs()).find(x => x.wake?.outcome !== "ask" && x.turns.some(t => t.role === "tars" && !t.rating));
  const reply = c?.turns.find(t => t.role === "tars" && !t.rating);
  if (!c || !reply) throw new Error("the demo log needs an unrated reply");
  const saved = async () => (await A.conv(c.id)).turns.find(t => t.id === reply.id)?.rating;
  const good = turn(reply.id).getByRole("button", { name: /^Good( answer)?$/ });
  await good.click();
  await expect(toast()).toHaveText(/^Rated good/);
  await expect.poll(saved).toBe("good");
  await expect(good).toHaveAttribute("aria-pressed", "true");
  await undo().click();
  await expect.poll(saved).toBeNull();
});

test("correcting a transcript saves it; saving what TARS heard (with Enter) clears it", async () => {
  const c = (await A.convs()).find(x => x.turns.some(t => t.role === "person" && !t.corrected_text && t.has_audio));
  const said = c?.turns.find(t => t.role === "person" && !t.corrected_text && t.has_audio);
  if (!c || !said) throw new Error("the demo log needs an uncorrected person turn");
  const saved = async () => (await A.conv(c.id)).turns.find(t => t.id === said.id)?.corrected_text;
  await open(c.id);
  await turn(said.id).getByRole("button", { name: "Fix text" }).click();
  const box = turn(said.id).getByLabel("What was said");
  await expect(box).toBeFocused();
  await expect(box).toHaveValue(said.text);
  await box.fill(said.text + " please");
  await turn(said.id).getByRole("button", { name: "Save" }).click();
  await expect(toast()).toHaveText(/^Transcript fixed/);
  await expect.poll(saved).toBe(said.text + " please");
  await expect(turn(said.id).locator(".fixed-note")).toContainText(said.text);
  await turn(said.id).getByRole("button", { name: "Fix text" }).click();
  await box.fill(said.text);
  await box.press("Enter");
  await expect.poll(saved).toBeNull();
});

test("ticking a list entry saves it and marks the list seen", async () => {
  await nav("Sent").click();
  const items = await A.items();
  const list = items.find(i => i.kind === "list" && !i.seen) ?? items.find(i => i.kind === "list");
  if (!list) throw new Error("the demo log needs a list");
  const box = item(list.id).getByRole("checkbox").nth(1);
  const was = (await box.getAttribute("aria-checked")) === "true";
  await box.click();
  await expect
    .poll(async () => {
      const now = (await A.items()).find(i => i.id === list.id);
      return now?.entries?.[1].done === !was && now.seen;
    })
    .toBe(true);
});

test("a note opens in the wide dialog, formatted, and that marks it seen", async () => {
  const note = (await A.items()).find(i => i.kind === "note");
  if (!note) throw new Error("the demo log needs a note");
  await item(note.id).getByRole("button", { name: "Open" }).click();
  await expect(dialog()).toHaveClass(/wide/);
  await expect(dialog().getByRole("heading")).toHaveText(note.title);
  await expect(dialog().locator(".prose li, .prose p").first()).toBeVisible();
  await expect.poll(async () => (await A.items()).find(i => i.id === note.id)?.seen).toBe(true);
  await dialog().getByRole("button", { name: "Done" }).click();
  await expect(dialog()).toBeHidden();
});

test("Sent lists everything", async () => {
  await expect(page.locator(".item")).toHaveCount((await A.items()).length);
});

test("“The conversation” goes to it, opened", async () => {
  const link = (await A.items()).find(i => i.kind === "link" && i.conversation_id != null);
  if (!link?.conversation_id) throw new Error("the demo log needs a link from a conversation");
  await openMenu(item(link.id).getByRole("button", { name: /^More for/ }));
  await menuItem("The conversation").click();
  await expect(nav("Home")).toHaveAttribute("aria-current", "page");
  const target = conv(link.conversation_id);
  await expect(target.locator(".panel")).toBeVisible();
  await expect
    .poll(async () => {
      const top = await target.evaluate(el => el.getBoundingClientRect().top);
      const atBottom = await page.evaluate(() => scrollY + innerHeight >= document.documentElement.scrollHeight - 2);
      return Math.abs(top - 84) < 4 || atBottom;
    })
    .toBe(true);
});

test("a sent image opens and downloads; deleting an item removes it", async () => {
  await nav("Sent").click();
  const file = (await A.items()).find(i => i.kind === "file" && (i.mime || "").startsWith("image/"));
  if (!file) throw new Error("the demo log needs a sent image");
  const image = item(file.id).locator("img");
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
  await expect(item(file.id).getByRole("link", { name: "Download" })).toHaveAttribute(
    "href",
    `/api/items/${file.id}/file`,
  );
  await item(file.id)
    .getByRole("button", { name: /^Show / })
    .click();
  await expect(dialog().getByRole("img")).toBeVisible();
  await dialog().getByRole("button", { name: "Done" }).click();
  await openMenu(item(file.id).getByRole("button", { name: /^More for/ }));
  await menuItem("Delete").click();
  await dialog().getByRole("button", { name: "Delete" }).click();
  await expect(toast()).toHaveText("Deleted.");
  await expect(item(file.id)).toHaveCount(0);
  expect((await A.items()).some(i => i.id === file.id)).toBe(false);
});

test("the Household filter shows only the house's things", async () => {
  await filter("Household").click();
  const labels = await page.locator(".item > .kind-line").allTextContents();
  expect(labels.length, "household things in the demo log").toBeGreaterThan(0);
  for (const label of labels) expect(label).toContain("for the household");
  await filter("Everyone").click();
});

test("an opened conversation: not for TARS, who was talking, delete", async () => {
  await nav("Home").click();
  const convs = await A.convs();
  const clusters = await A.clusters();
  const target = convs.find(c => c.wake?.has_request_audio && c.speaker?.name);
  const other = clusters.find(c => c.name && c.id !== target?.speaker?.cluster_id);
  if (!target?.wake || !target.speaker?.name || !other?.name) {
    throw new Error("the demo log needs a named speaker's conversation with request audio, and another named voice");
  }
  const wake = async () => (await A.conv(target.id)).wake;
  const panel = conv(target.id).locator(".panel");
  if (!(await panel.count())) await open(target.id);

  await panel.getByRole("button", { name: "Not meant for TARS" }).click();
  await expect.poll(async () => (await wake())?.label).toBe("not_real");
  await panel.getByRole("button", { name: "Meant for TARS after all" }).click();
  await expect.poll(async () => (await wake())?.label).toBeNull();

  await openMenu(panel.getByRole("button", { name: "Who was talking" }));
  await expect(menu().getByRole("menuitemradio", { checked: true })).toHaveText(target.speaker.name);
  await menuItem(other.name).click();
  await expect(toast()).toHaveText(new RegExp(`^Moved to ${other.name}`));
  expect((await wake())?.cluster_id).toBe(other.id);
  await expect(panel.locator(".panel-head .who")).toHaveText(new RegExp(`^${other.name}, `));
  await expect(panel.locator(".p-turn .said b").first()).toHaveText(other.name);
  const sent = (await A.items()).filter(i => i.conversation_id === target.id && i.scope === "person");
  for (const i of sent) expect(i.for?.name).toBe(other.name);

  await panel.getByRole("button", { name: "Delete" }).click();
  await expect(dialog().getByRole("heading")).toHaveText("Delete this conversation?");
  await dialog().getByRole("button", { name: "Delete" }).click();
  await expect(toast()).toHaveText("Deleted the conversation.");
  expect((await A.convs()).some(c => c.id === target.id)).toBe(false);
  expect(await wake()).toBeUndefined();
});

test("a name prompt: Cancel doesn't save", async () => {
  await nav("Voices").click();
  const voice = page.locator(".voice", { has: page.getByRole("button", { name: "Name it" }) }).first();
  await expect(voice, "an unnamed voice in the demo log").not.toHaveCount(0);
  await voice.getByRole("button", { name: "Name it" }).click();
  await dialog().getByRole("textbox").fill("Nobody");
  await dialog().getByRole("button", { name: "Cancel" }).click();
  await expect(dialog()).toBeHidden();
  expect((await A.clusters()).some(c => c.name === "Nobody")).toBe(false);
});

test("an unknown voice is named from its conversation, with Enter", async () => {
  await nav("Home").click();
  const anon = (await A.convs()).find(c => c.speaker?.cluster_id != null && !c.speaker.name);
  const voiceId = anon?.speaker?.cluster_id;
  if (!anon || voiceId == null) throw new Error("the demo log needs a conversation with an unnamed voice");
  await conv(anon.id).getByRole("button", { name: "Name this voice" }).first().click();
  await dialog().getByRole("textbox").fill("Guest");
  await dialog().getByRole("textbox").press("Enter");
  await expect(toast()).toHaveText(/^Saved\./);
  expect((await A.clusters()).find(c => c.id === voiceId)?.name).toBe("Guest");
  await expect(conv(anon.id).locator(".said b").first()).toHaveText("Guest");
});

test("filtering by a person shows their conversations only", async () => {
  const stacey = (await A.clusters()).find(c => c.name === "Stacey");
  if (!stacey) throw new Error("the demo log needs Stacey");
  await filter("Stacey").click();
  const hers = (await A.convs()).filter(c => c.speaker?.cluster_id === stacey.id).length;
  await expect(page.locator(".conv")).toHaveCount(hers);
  await filter("Everyone").click();
});

test("every person turn's clip serves as audio/wav", async () => {
  const turns = (await A.convs()).flatMap(c => c.turns).filter(t => t.role === "person" && t.has_audio);
  expect(turns.length, "person turns with audio in the demo log").toBeGreaterThan(0);
  for (const t of turns) {
    const r = await page.request.get(`/api/audio/turn/${t.id}`);
    expect(`${r.status()} ${r.headers()["content-type"]}`).toBe("200 audio/wav");
  }
});

test("Review shows the unclear wakes; answering one updates the counts", async () => {
  await nav("Review").click();
  const n = reviewTodo(await A.events());
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`${n} to check`);
  const first = page.getByRole("group", { name: "Was it really hey TARS?" }).first();
  await first.getByRole("button", { name: "hey TARS" }).click();
  await expect.poll(async () => reviewTodo(await A.events())).toBe(n - 1);
  await expect(nav("Review")).toHaveText(n - 1 ? `Review ${n - 1}` : "Review");
});

test("nothing moves by itself: a new item opened from Home stays there until Refresh", async () => {
  await nav("Home").click();
  const fresh = (await A.items()).find(i => !i.seen);
  if (!fresh) throw new Error("the demo log needs an unseen item on Home");
  const row = page.locator(".rows .row", { hasText: fresh.title });
  await row.click();
  await expect(nav("Sent")).toHaveAttribute("aria-current", "page");
  await expect.poll(async () => (await A.items()).find(i => i.id === fresh.id)?.seen).toBe(true);
  await nav("Home").click();
  await expect(row).toHaveCount(1);
  await expect(row.locator(".new")).toHaveCount(0);
  await refresh();
  await expect(row).toHaveCount(0);
});

test("an answered wake stays under To check, and Refresh moves it to Reviewed", async () => {
  await nav("Review").click();
  const unanswered = page.locator(`main > .wakes > .wake:not(:has(${answered}))`);
  await expect(unanswered, "an unanswered wake to check").not.toHaveCount(0);
  const rows = await toCheck().count();
  const before = reviewTodo(await A.events());
  await unanswered.first().getByRole("button", { name: "Not it" }).click();
  await expect.poll(async () => reviewTodo(await A.events())).toBe(before - 1);
  await expect(toCheck()).toHaveCount(rows);
  await refresh();
  await expect(toCheck()).toHaveCount(reviewTodo(await A.events()));
});

test("clearing an answer in Reviewed keeps the row there until Refresh", async () => {
  const rows = await reviewed().count();
  await reviewed().first().locator(answered).click();
  await expect(toast()).toHaveText("Answer cleared.");
  await expect(reviewed()).toHaveCount(rows);
});

test("Models shows the pair in use and how it tested, and Use this switches back", async () => {
  await nav("Models").click();
  const before = await A.models();
  if (!before.active || !before.results) throw new Error("the demo log needs a trained pair in use, with results");
  await expect(page.locator(".kv dd").first()).toHaveText(new RegExp(`^${before.active.version}`));
  await expect(page.locator(".cmp tbody tr")).toHaveCount(before.results.length);
  await page.locator(".history").getByRole("button", { name: "Use this" }).last().click();
  await dialog().getByRole("button", { name: "Use the installed models" }).click();
  await expect(toast()).toHaveText("Now using the installed models.");
  await expect.poll(async () => (await A.models()).active?.version).toBe("installed");
  await expect(page.locator(".kv dd").first()).toHaveText("installed");
});
