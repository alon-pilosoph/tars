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
  };
};
const reviewTodo = (events: TarsEvent[]) => events.filter(e => e.follow !== "asked" && !e.label).length;
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

test.describe.configure({ mode: "serial" });
let page: Page;
let A: ReturnType<typeof api>;
const conv = (id: number) => page.locator(`.conv[data-id="${id}"]`);
const card = (id: number) => page.locator(`.item[data-id="${id}"]`).first();
const turn = (id: number) => page.locator(`.turn[data-turn="${id}"]`);
const toast = () => page.locator(".toast > span"); // the message, without its Undo
const undo = () => page.getByRole("status").getByRole("button", { name: "Undo" });
const nav = (name: string) =>
  page.getByRole("navigation", { name: "Sections" }).getByRole("button", { name: new RegExp(`^${name}`) });
const menu = () => page.getByRole("menu");
const menuItem = (name: string | RegExp) =>
  menu().getByRole("menuitem", { name }).or(menu().getByRole("menuitemradio", { name })).first();
const dialog = () => page.getByRole("dialog");
const group = (title: string) =>
  page.locator("section", { has: page.getByRole("heading", { level: 2, name: new RegExp(`^${title}`) }) });
const tabCount = (n: number) => (n ? String(n) : "");
const filter = (name: string) => page.getByRole("group", { name: "Show" }).getByRole("button", { name });
async function openMenu(button: Locator) {
  await button.click();
  await expect(menu()).toBeVisible();
}
async function refresh() {
  const button = page.getByRole("button", { name: "Refresh" });
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
  const toCheck = reviewTodo(await A.events());
  const convs = await A.convs();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`${unseen.length} new from TARS`);
  await expect(nav("Sent")).toHaveText(`Sent${tabCount(unseen.length)}`);
  await expect(nav("Review")).toHaveText(`Review${tabCount(toCheck)}`);
  await expect(page).toHaveTitle(`TARS (${unseen.length})`);
  await expect(page.locator(".item-grid > .item.unseen")).toHaveCount(unseen.length);
  await expect(page.locator(".conv")).toHaveCount(convs.length);
  await expect(page.getByRole("button", { name: /to check/ })).toContainText(`${plural(toCheck, "wake")} to check`);
});

test("a long conversation folds its middle, and expands", async () => {
  const long = (await A.convs()).find(c => c.turns.length > 4 && !c.turns.slice(2, -1).some(t => t.items?.length));
  if (!long) throw new Error("the demo log needs a conversation with more than 4 turns and nothing sent in its middle");
  const fold = conv(long.id).getByRole("button", { name: /more turns?$/ });
  await expect(fold).toHaveText(`${long.turns.length - 3} more turns`);
  await fold.click();
  await expect(conv(long.id).locator(".turn")).toHaveCount(long.turns.length);
  await expect(fold).toHaveCount(0);
});

test("each answer shows how long it took and who wrote it, and a failed one says where it failed", async () => {
  const tars = (await A.convs()).flatMap(c => c.turns).filter(t => t.role === "tars");
  const timed = tars.find(t => t.timings?.total != null && t.answered_by === "quick");
  const failed = tars.find(t => t.failed_at === "llm");
  if (!timed || !failed) throw new Error("the demo log needs a timed answer and a failed one");
  await expect(turn(timed.id).locator(".turn-meta")).toHaveText(`${timed.timings!.total!.toFixed(1)} s · Qwen`);
  await expect(turn(failed.id).locator(".turn-failed")).toContainText("Failed while writing the answer");
  await expect(turn(failed.id).locator(".turn-error")).toHaveText(failed.error!);
});

test("Reminders: one waiting for a got it is acknowledged on the page; a new one is set; one is cancelled", async () => {
  const list = async () => (await page.request.get("/api/reminders").then(r => r.json())) as RemindersInfo;
  const row = (id: number) => page.locator(`.rem[data-id="${id}"]`);
  await nav("Reminders").click();
  const waiting = (await list()).reminders.find(r => r.status === "waiting");
  if (!waiting) throw new Error("the demo log needs a reminder waiting for a got it");
  await expect(nav("Reminders")).toContainText("1");
  await expect(row(waiting.id).locator(".rem-says")).toHaveText(`“${waiting.says}”`);
  await row(waiting.id).getByRole("button", { name: "Got it" }).click();
  await expect(row(waiting.id).locator(".rem-status")).toContainText("Acknowledged on this page");
  const acked = (await list()).reminders.find(r => r.id === waiting.id)!;
  expect([acked.status, acked.acked_via]).toEqual(["acknowledged", "web"]);

  await page.getByRole("button", { name: "New reminder" }).click();
  const form = page.getByRole("form", { name: "New reminder" });
  await form.getByRole("button", { name: "Message" }).click();
  await form.getByLabel("What TARS says").fill("dinner's at eight");
  await form.getByLabel("For").fill("alon");
  await form.getByLabel("From (optional)").fill("Stacey");
  await form.getByLabel("When Alon is next heard").check();
  await form.getByLabel("For").fill("bo"); // someone TARS doesn't know by voice: no time was picked, so nothing is set
  await form.getByRole("button", { name: "Set it" }).click();
  await expect(toast()).toHaveText(/^TARS doesn't know Bo's voice yet/);
  expect((await list()).reminders.some(r => r.text === "dinner's at eight")).toBe(false);
  await form.getByLabel("For").fill("alon");
  await form.getByLabel("When Alon is next heard").check();
  await form.getByRole("button", { name: "Set it" }).click();
  await expect(toast()).toHaveText(/^Set\./);
  const made = (await list()).reminders.find(r => r.text === "dinner's at eight")!;
  expect([made.for_name, made.from_name, made.due, made.set_via]).toEqual(["alon", "Stacey", null, "web"]);
  await expect(row(made.id).locator(".rem-status")).toHaveText("When Alon is next heard.");

  await row(made.id).getByRole("button", { name: "Stop…" }).click();
  await dialog().getByRole("button", { name: "Stop it" }).click();
  await expect.poll(async () => (await list()).reminders.find(r => r.id === made.id)?.status).toBe("cancelled");
  await nav("Home").click();
});

test("rating a reply saves it, and Undo clears it", async () => {
  const c = (await A.convs()).find(x => x.turns.some(t => t.role === "tars" && !t.rating));
  const reply = c?.turns.find(t => t.role === "tars" && !t.rating);
  if (!c || !reply) throw new Error("the demo log needs an unrated reply");
  const saved = async () => (await A.conv(c.id)).turns.find(t => t.id === reply.id)?.rating;
  await turn(reply.id).getByRole("button", { name: "Good" }).click();
  await expect(toast()).toHaveText(/^Rated good/);
  await expect.poll(saved).toBe("good");
  await expect(turn(reply.id).getByRole("button", { name: "✓ Good", pressed: true })).toBeVisible();
  await undo().click();
  await expect.poll(saved).toBeNull();
});

test("correcting a transcript saves it; saving what TARS heard (with Enter) clears it", async () => {
  const c = (await A.convs()).find(x => x.turns.some(t => t.role === "person" && !t.corrected_text));
  const said = c?.turns.find(t => t.role === "person" && !t.corrected_text);
  if (!c || !said) throw new Error("the demo log needs an uncorrected person turn");
  const saved = async () => (await A.conv(c.id)).turns.find(t => t.id === said.id)?.corrected_text;
  await turn(said.id).getByRole("button", { name: "Fix text" }).click();
  const box = turn(said.id).getByLabel("What was said");
  await expect(box).toBeFocused();
  await expect(box).toHaveValue(said.text);
  await box.fill(said.text + " please");
  await turn(said.id).getByRole("button", { name: "Save" }).click();
  await expect(toast()).toHaveText(/^Transcript fixed/);
  await expect.poll(saved).toBe(said.text + " please");
  await expect(turn(said.id).locator(".fixed")).toContainText(said.text);
  await turn(said.id).getByRole("button", { name: "Edit fix" }).click();
  await box.fill(said.text);
  await box.press("Enter");
  await expect.poll(saved).toBeNull();
});

test("ticking a list entry saves it and marks the list seen", async () => {
  const items = await A.items();
  const list = items.find(i => i.kind === "list" && !i.seen) ?? items.find(i => i.kind === "list");
  if (!list) throw new Error("the demo log needs a list");
  const box = card(list.id).getByRole("checkbox").nth(1);
  const was = await box.isChecked();
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
  await card(note.id).getByRole("button", { name: "Open" }).click();
  await expect(dialog()).toHaveClass(/wide/);
  await expect(dialog().getByRole("heading")).toHaveText(note.title);
  await expect(dialog().locator(".md li, .md p").first()).toBeVisible();
  await expect.poll(async () => (await A.items()).find(i => i.id === note.id)?.seen).toBe(true);
  await dialog().getByRole("button", { name: "Done" }).click();
  await expect(dialog()).toHaveCount(0);
});

test("Sent lists everything, with what was new at the last Refresh under New", async () => {
  await nav("Sent").click();
  const items = await A.items();
  await expect(page.locator(".item")).toHaveCount(items.length);
  for (const i of items.filter(x => !x.seen)) {
    await expect(group("New").locator(`.item[data-id="${i.id}"]`)).toHaveCount(1);
  }
});

test("“Show the conversation” goes to it", async () => {
  const link = (await A.items()).find(i => i.kind === "link" && i.conversation_id != null);
  if (!link?.conversation_id) throw new Error("the demo log needs a link from a conversation");
  await openMenu(card(link.id).getByRole("button", { name: /^More for/ }));
  await menuItem("Show the conversation").click();
  await expect(nav("Home")).toHaveAttribute("aria-current", "page");
  const target = conv(link.conversation_id);
  await expect
    .poll(async () => {
      const top = await target.evaluate(el => el.getBoundingClientRect().top);
      const below = await page.evaluate(
        () => (document.querySelector(".top")?.getBoundingClientRect().height ?? 0) + 16,
      );
      const atBottom = await page.evaluate(() => scrollY + innerHeight >= document.documentElement.scrollHeight - 2);
      return Math.abs(top - below) < 4 || atBottom;
    })
    .toBe(true);
});

test("a sent image opens and downloads; deleting an item removes it", async () => {
  await nav("Sent").click();
  const file = (await A.items()).find(i => i.kind === "file" && (i.mime || "").startsWith("image/"));
  if (!file) throw new Error("the demo log needs a sent image");
  const image = card(file.id).locator("img"); // inside its button, so not an img to assistive tech
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
  await expect(card(file.id).getByRole("link", { name: "Download" })).toHaveAttribute(
    "href",
    `/api/items/${file.id}/file`,
  );
  await card(file.id)
    .getByRole("button", { name: /^Show / })
    .click();
  await expect(dialog().getByRole("img")).toBeVisible();
  await dialog().getByRole("button", { name: "Done" }).click();
  await openMenu(card(file.id).getByRole("button", { name: /^More for/ }));
  await menuItem("Delete file…").click();
  await dialog().getByRole("button", { name: "Delete" }).click();
  await expect(toast()).toHaveText("File deleted.");
  await expect(page.locator(`.item[data-id="${file.id}"]`)).toHaveCount(0);
  expect((await A.items()).some(i => i.id === file.id)).toBe(false);
});

test("the Household filter shows only the house's things", async () => {
  await filter("Household").click();
  const labels = await page.locator(".item .item-for").allTextContents();
  expect(labels.length, "household things in the demo log").toBeGreaterThan(0);
  for (const label of labels) expect(label).toBe("For the household");
  await filter("Everyone").click();
});

test("a conversation's menu: not for TARS, who was talking, delete", async () => {
  await nav("Home").click();
  const convs = await A.convs();
  const clusters = await A.clusters();
  const target = convs.find(c => c.wake?.has_request_audio && c.speaker?.name);
  const other = clusters.find(c => c.name && c.id !== target?.speaker?.cluster_id);
  if (!target?.wake || !target.speaker?.name || !other?.name) {
    throw new Error("the demo log needs a named speaker's conversation with request audio, and another named voice");
  }
  const wake = async () => (await A.conv(target.id)).wake;
  const more = conv(target.id).getByRole("button", { name: "More for this conversation" });

  await openMenu(more);
  const who = menu().getByRole("group", { name: "Who was talking" });
  await expect(who).toBeVisible();
  await expect(who.getByRole("menuitemradio", { checked: true })).toHaveText(new RegExp(`^${target.speaker.name}`));
  await menuItem("Not meant for TARS").click();
  await expect.poll(async () => (await wake())?.label).toBe("not_real");
  await openMenu(more);
  await menuItem("It was for TARS").click();
  await expect.poll(async () => (await wake())?.label).toBeNull();

  await openMenu(more);
  await menuItem(new RegExp(`^${other.name}`)).click();
  await expect(toast()).toHaveText(new RegExp(`^Moved to ${other.name}`));
  expect((await wake())?.cluster_id).toBe(other.id);
  await expect(conv(target.id).locator(".conv-who")).toHaveText(other.name);
  await expect(conv(target.id).locator(".turn.person .turn-who").first()).toHaveText(other.name);
  const sent = (await A.items()).filter(i => i.conversation_id === target.id && i.scope === "person");
  for (const i of sent) expect(i.for?.name).toBe(other.name);

  await openMenu(more);
  await menuItem("Delete conversation…").click();
  await expect(dialog().getByRole("heading")).toHaveText("Delete this conversation?");
  await dialog().getByRole("button", { name: "Delete" }).click();
  await expect(toast()).toHaveText("Conversation deleted.");
  expect((await A.convs()).some(c => c.id === target.id)).toBe(false);
  expect(await wake()).toBeUndefined();
});

test("a name prompt: Cancel doesn't save", async () => {
  await nav("Voices").click();
  const voice = page.locator(".voice", { has: page.locator(".voice-foot") }).first();
  await expect(voice, "an unnamed voice in the demo log").not.toHaveCount(0);
  await voice.getByRole("button", { name: "Name…" }).click();
  await dialog().getByRole("textbox").fill("Nobody");
  await dialog().getByRole("button", { name: "Cancel" }).click();
  await expect(dialog()).toHaveCount(0);
  expect((await A.clusters()).some(c => c.name === "Nobody")).toBe(false);
});

test("an unknown voice is named from its conversation, with Enter", async () => {
  await nav("Home").click();
  const anon = (await A.convs()).find(c => c.speaker?.cluster_id != null && !c.speaker.name);
  const voiceId = anon?.speaker?.cluster_id;
  if (!anon || voiceId == null) throw new Error("the demo log needs a conversation with an unnamed voice");
  await conv(anon.id).getByRole("button", { name: "Name this voice" }).click();
  await dialog().getByRole("textbox").fill("Guest");
  await dialog().getByRole("textbox").press("Enter");
  await expect(toast()).toHaveText(/^Saved\./);
  expect((await A.clusters()).find(c => c.id === voiceId)?.name).toBe("Guest");
  await expect(conv(anon.id).locator(".conv-who")).toHaveText("Guest");
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
  await expect(group("To check").getByRole("heading", { level: 2 })).toHaveText(new RegExp(`${n}$`));
  const firstAnswer = page.getByRole("group", { name: "Was it really hey TARS?" }).first();
  await firstAnswer.getByRole("button", { name: "hey TARS" }).click();
  await expect.poll(async () => reviewTodo(await A.events())).toBe(n - 1);
  await expect(nav("Review")).toHaveText(`Review${tabCount(n - 1)}`);
});

test("nothing moves by itself: a ticked list stays in Home's strip until Refresh", async () => {
  await nav("Home").click();
  const items = await A.items();
  let fresh: Item | undefined;
  for (const i of items.filter(x => x.kind === "list" && !x.seen)) {
    if (await page.locator(`.item-grid .item[data-id="${i.id}"]`).count()) fresh = i;
  }
  if (!fresh) throw new Error("the demo log needs an unseen list on Home");
  const id = fresh.id;
  const strip = page.locator(`.item-grid .item[data-id="${id}"]`);
  await strip.getByRole("checkbox").first().click();
  await expect.poll(async () => (await A.items()).find(i => i.id === id)?.seen).toBe(true);
  await expect(strip).toHaveCount(1);
  await expect(strip.locator(".new")).toHaveCount(0);
  await refresh();
  await expect(strip).toHaveCount(0);
});

test("an answered wake stays under To check, and Refresh moves it to Reviewed", async () => {
  await nav("Review").click();
  const toCheck = group("To check");
  const unanswered = toCheck.locator("article.wake-row:not(.answered)");
  await expect(unanswered, "an unanswered wake under To check").not.toHaveCount(0);
  const rows = await toCheck.locator("article.wake-row").count();
  const before = reviewTodo(await A.events());
  await unanswered.first().getByRole("button", { name: "Not it" }).click();
  await expect.poll(async () => reviewTodo(await A.events())).toBe(before - 1);
  await expect(toCheck.locator("article.wake-row")).toHaveCount(rows);
  await refresh();
  const left = reviewTodo(await A.events());
  if (left) await expect(toCheck.locator("article.wake-row")).toHaveCount(left);
  else await expect(group("To check")).toHaveCount(0);
});

test("clearing an answer in Reviewed keeps the row there until Refresh", async () => {
  const reviewed = group("Reviewed");
  const row = reviewed.locator("article.wake-row.answered").first();
  const rows = await reviewed.locator("article.wake-row").count();
  await row.getByRole("button", { pressed: true }).click();
  await expect(toast()).toHaveText("Answer cleared.");
  await expect(reviewed.locator("article.wake-row")).toHaveCount(rows);
});

test("Models shows the pair in use and how it tested, and “Use this…” switches back", async () => {
  await nav("Models").click();
  const before = await A.models();
  if (!before.active || !before.results) throw new Error("the demo log needs a trained pair in use, with results");
  await expect(page.locator(".version")).toHaveText(before.active.version);
  await expect(page.locator(".compare tbody tr")).toHaveCount(before.results.length);
  await page.locator(".hist").getByRole("button", { name: "Use this…" }).first().click();
  await dialog().getByRole("button", { name: "Use this version" }).click();
  await expect(toast()).toHaveText("Now using the installed models.");
  await expect.poll(async () => (await A.models()).active?.version).toBe("installed");
  await expect(page.locator(".version")).toHaveText("installed");
});
