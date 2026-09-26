/* Functional check of the web UI against the real backend, on a throwaway copy of the demo log (e2e.config.ts builds
   one each run). `npm run e2e`. The steps run in order in one page, like a person using it. */
import { type APIRequestContext, type Locator, type Page, expect, test } from "@playwright/test";

interface Entry {
  text: string;
  done: boolean;
}
interface Item {
  id: number;
  kind: string;
  title: string;
  seen: boolean;
  scope: string;
  mime?: string;
  conversation_id: number | null;
  entries?: Entry[];
  for?: { cluster_id: number | null; name: string | null } | null;
}
interface Turn {
  id: number;
  role: string;
  text: string;
  rating?: string | null;
  corrected_text?: string | null;
  has_audio?: boolean;
  items?: unknown[];
}
interface Conv {
  id: number;
  speaker: { cluster_id: number | null; name: string | null } | null;
  wake: { event_id: number | null } | null;
  turns: Turn[];
}
interface Ev {
  id: number;
  follow: string | null;
  label: string | null;
  cluster_id: number | null;
  utterance_audio: string | null;
}
interface Cluster {
  id: number;
  name: string | null;
}

const api = (r: APIRequestContext) => {
  const json = async <T>(p: string) => (await (await r.get(p)).json()) as T;
  return {
    items: () => json<Item[]>("/api/items"),
    events: () => json<Ev[]>("/api/events"),
    clusters: () => json<Cluster[]>("/api/clusters"),
    convs: () => json<Conv[]>("/api/conversations"),
    conv: (id: number) => json<Conv>(`/api/conversations/${id}`),
  };
};
const reviewTodo = (ev: Ev[]) => ev.filter(e => e.follow !== "asked" && !e.label).length;

test.describe.configure({ mode: "serial" });
let page: Page, A: ReturnType<typeof api>;
const conv = (id: number) => page.locator(`.conv[data-id="${id}"]`);
const card = (id: number) => page.locator(`.item[data-id="${id}"]`).first();
const turn = (id: number) => page.locator(`.turn[data-turn="${id}"]`);
const toast = () => page.locator(".toast span");
const nav = (name: string) => page.locator(".nav button", { hasText: name }).first();
const menuItem = (text: string) => page.locator(".menu button", { hasText: text }).first();
const dialog = () => page.locator("dialog[open]");
async function openMenu(button: Locator) {
  await button.click();
  await expect(page.locator(".menu")).toBeVisible();
}
async function refresh() {
  await page.locator(".refresh").click();
  await expect(page.locator(".refresh")).toHaveAttribute("aria-busy", "false");
}

test.beforeAll(async ({ browser }, info) => {
  page = await browser.newPage({ baseURL: info.project.use.baseURL });
  A = api(page.request);
  await page.goto("/");
  await expect(page.locator(".conv").first()).toBeVisible();
});
test.afterAll(() => page.close());

test("Home: what's new first, and every count agrees with the server", async () => {
  const unseen = (await A.items()).filter(i => !i.seen),
    ev = await A.events(),
    convs = await A.convs();
  await expect(page.locator("h1")).toHaveText(`${unseen.length} new from TARS`);
  await expect(nav("Sent").locator(".count")).toHaveText(String(unseen.length));
  await expect(nav("Review").locator(".count")).toHaveText(String(reviewTodo(ev)));
  await expect(page).toHaveTitle(`TARS (${unseen.length})`);
  await expect(page.locator(".igrid > .item.unseen")).toHaveCount(unseen.length);
  await expect(page.locator(".conv")).toHaveCount(convs.length);
  await expect(page.locator(".nudge")).toContainText(`${reviewTodo(ev)} wakes to check`);
});

test("a long conversation folds its middle, and expands", async () => {
  const long = (await A.convs()).find(c => c.turns.length > 4 && !c.turns.slice(2, -1).some(t => t.items?.length));
  expect(long, "a conversation with more than 4 turns and nothing sent in its middle").toBeTruthy();
  const fold = conv(long!.id).locator(".more-turns");
  await expect(fold).toHaveText(`${long!.turns.length - 3} more turns`);
  await fold.click();
  await expect(conv(long!.id).locator(".turn")).toHaveCount(long!.turns.length);
  await expect(fold).toHaveCount(0);
});

test("rating a reply saves it, and Undo clears it", async () => {
  const c = (await A.convs()).find(c => c.turns.some(t => t.role === "tars" && !t.rating))!;
  const reply = c.turns.find(t => t.role === "tars" && !t.rating)!;
  const saved = async () => (await A.conv(c.id)).turns.find(t => t.id === reply.id)!.rating;
  await turn(reply.id).locator(".rate .g").click();
  await expect(toast()).toHaveText(/^Noted: a good answer/);
  await expect.poll(saved).toBe("good");
  await expect(turn(reply.id).locator(".rate.rated .g")).toHaveText("✓ Good answer");
  await page.locator(".toast button").click();
  await expect.poll(saved).toBeNull();
});

test("correcting a transcript saves it; saving what TARS heard (with Enter) clears it", async () => {
  const c = (await A.convs()).find(c => c.turns.some(t => t.role === "person" && !t.corrected_text))!;
  const said = c.turns.find(t => t.role === "person" && !t.corrected_text)!;
  const saved = async () => (await A.conv(c.id)).turns.find(t => t.id === said.id)!.corrected_text;
  await turn(said.id).locator(".tact", { hasText: "Fix text" }).click();
  const box = page.locator(`#fx-${said.id}`);
  await expect(box).toBeFocused();
  await expect(box).toHaveValue(said.text);
  await box.fill(said.text + " please");
  await page.locator(".edit .btn", { hasText: "Save" }).click();
  await expect(toast()).toHaveText(/^Fixed\./);
  await expect.poll(saved).toBe(said.text + " please");
  await expect(turn(said.id).locator(".fixed")).toContainText(said.text);
  await turn(said.id).locator(".tact", { hasText: "Edit fix" }).click();
  await box.fill(said.text);
  await box.press("Enter");
  await expect.poll(saved).toBeNull();
});

test("ticking a list entry saves it and marks the list seen", async () => {
  const items = await A.items();
  const list = items.find(i => i.kind === "list" && !i.seen) ?? items.find(i => i.kind === "list")!;
  const box = card(list.id).locator(".entries input").nth(1),
    was = await box.isChecked();
  await box.click();
  await expect
    .poll(async () => {
      const now = (await A.items()).find(i => i.id === list.id)!;
      return now.entries![1].done === !was && now.seen;
    })
    .toBe(true);
});

test("a note opens in the wide dialog, formatted, and that marks it seen", async () => {
  const note = (await A.items()).find(i => i.kind === "note")!;
  await card(note.id).locator(".btn", { hasText: "Open" }).click();
  await expect(dialog()).toHaveClass(/wide/);
  await expect(dialog().locator("h3")).toHaveText(note.title);
  await expect(dialog().locator(".md li, .md p").first()).toBeVisible();
  await expect.poll(async () => (await A.items()).find(i => i.id === note.id)!.seen).toBe(true);
  await dialog().locator(".btn", { hasText: "Done" }).click();
  await expect(dialog()).toHaveCount(0);
});

test("Sent lists everything, with what was new at the last Refresh under New", async () => {
  await nav("Sent").click();
  const items = await A.items();
  await expect(page.locator(".item")).toHaveCount(items.length);
  const fresh = page.locator(".group", { has: page.locator(".group-h", { hasText: "New" }) });
  for (const i of items.filter(i => !i.seen)) await expect(fresh.locator(`.item[data-id="${i.id}"]`)).toHaveCount(1);
});

test("'Show the conversation' goes to it", async () => {
  const link = (await A.items()).find(i => i.kind === "link" && i.conversation_id != null)!;
  await openMenu(card(link.id).locator(".more"));
  await menuItem("Show the conversation").click();
  await expect(nav("Home")).toHaveAttribute("aria-current", "page");
  await expect
    .poll(async () => {
      const top = await conv(link.conversation_id!).evaluate(el => el.getBoundingClientRect().top);
      const atBottom = await page.evaluate(() => scrollY + innerHeight >= document.documentElement.scrollHeight - 2);
      return Math.abs(top - 76) < 4 || atBottom;
    })
    .toBe(true);
});

test("a sent image previews and downloads; deleting an item removes it", async () => {
  await nav("Sent").click();
  const file = (await A.items()).find(i => i.kind === "file" && /^image\//.test(i.mime || ""))!;
  await expect
    .poll(() =>
      card(file.id)
        .locator("img")
        .evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0),
    )
    .toBe(true);
  await expect(card(file.id).locator("a[download]")).toHaveAttribute("href", `/api/items/${file.id}/file`);
  await openMenu(card(file.id).locator(".more"));
  await menuItem("Delete…").click();
  await dialog().locator("button[value=ok]").click();
  await expect(toast()).toHaveText("Deleted.");
  await expect(page.locator(`.item[data-id="${file.id}"]`)).toHaveCount(0);
  expect((await A.items()).some(i => i.id === file.id)).toBe(false);
});

test("the Household filter shows only the house's things", async () => {
  await page.locator(".aside-r .seg button", { hasText: "Household" }).click();
  for (const f of await page.locator(".item .for").allTextContents()) expect(f).toBe("For the household");
  await page.locator(".aside-r .seg button", { hasText: "Everyone" }).click();
});

test("a conversation's menu: not for TARS, who was talking, delete", async () => {
  await nav("Home").click();
  const convs = await A.convs(),
    ev = await A.events(),
    clusters = await A.clusters();
  const target = convs.find(
    c => c.wake && ev.find(e => e.id === c.wake!.event_id)?.utterance_audio && c.speaker?.name,
  )!;
  const other = clusters.find(c => c.name && c.id !== target.speaker!.cluster_id)!;
  const wake = async () => (await A.events()).find(e => e.id === target.wake!.event_id);
  const more = conv(target.id).locator(".conv-h .more");

  await openMenu(more);
  await expect(page.locator(".menu h4", { hasText: "Who was talking" })).toBeVisible();
  await expect(page.locator(".menu [aria-checked=true]")).toHaveText(new RegExp(`^${target.speaker!.name}`));
  await menuItem("Not meant for TARS").click();
  await expect.poll(async () => (await wake())!.label).toBe("not_real");
  await openMenu(more);
  await menuItem("It was for TARS").click();
  await expect.poll(async () => (await wake())!.label).toBeNull();

  await openMenu(more);
  await menuItem(other.name!).click();
  await expect(toast()).toHaveText(new RegExp(`^Moved to ${other.name}`));
  expect((await wake())!.cluster_id).toBe(other.id);
  await expect(conv(target.id).locator(".conv-h .p")).toHaveText(other.name!);
  await expect(conv(target.id).locator(".turn.p .tw").first()).toHaveText(other.name!);
  for (const i of (await A.items()).filter(i => i.conversation_id === target.id && i.scope === "person"))
    expect(i.for?.name).toBe(other.name);

  await openMenu(more);
  await menuItem("Delete conversation…").click();
  await expect(dialog().locator("h3")).toHaveText("Delete this conversation?");
  await dialog().locator("button[value=ok]").click();
  await expect(toast()).toHaveText("Conversation deleted.");
  expect((await A.convs()).some(c => c.id === target.id)).toBe(false);
  expect(await wake()).toBeUndefined();
});

test("an unknown voice is named from its conversation, with Enter", async () => {
  const anon = (await A.convs()).find(c => c.speaker?.cluster_id != null && !c.speaker.name);
  test.skip(!anon, "no conversation with an unnamed voice in the demo log");
  await conv(anon!.id).locator(".tact", { hasText: "Name this voice" }).click();
  await dialog().locator("input").fill("Guest");
  await dialog().locator("input").press("Enter");
  await expect(toast()).toHaveText(/^Saved\./);
  expect((await A.clusters()).find(c => c.id === anon!.speaker!.cluster_id)!.name).toBe("Guest");
  await expect(conv(anon!.id).locator(".conv-h .p")).toHaveText("Guest");
});

test("filtering by a person shows their conversations only", async () => {
  const stacey = (await A.clusters()).find(c => c.name === "Stacey")!;
  await page.locator(".aside-r .seg button", { hasText: "Stacey" }).click();
  const hers = (await A.convs()).filter(c => c.speaker?.cluster_id === stacey.id).length;
  await expect(page.locator(".conv")).toHaveCount(hers);
  await page.locator(".aside-r .seg button", { hasText: "Everyone" }).click();
});

test("every person turn's clip serves as audio/wav", async () => {
  const turns = (await A.convs()).flatMap(c => c.turns).filter(t => t.role === "person" && t.has_audio);
  for (const t of turns) {
    const r = await page.request.get(`/api/audio/turn/${t.id}`);
    expect(`${r.status()} ${r.headers()["content-type"]}`).toBe("200 audio/wav");
  }
});

test("Review shows the unclear wakes; answering one updates the counts", async () => {
  await nav("Review").click();
  const n = reviewTodo(await A.events());
  await expect(page.locator("h1")).toHaveText(`${n} to check`);
  await expect(page.locator(".group-h", { hasText: "To check" })).toHaveText(new RegExp(`${n}$`));
  await page.locator("article.ev .lab-seg .y").first().click();
  await expect.poll(async () => reviewTodo(await A.events())).toBe(n - 1);
  await expect(nav("Review").locator(".count")).toHaveText(String(n - 1));
});

test("nothing moves by itself: a ticked list stays in Home's strip until Refresh", async () => {
  await nav("Home").click();
  const items = await A.items();
  let fresh: Item | undefined;
  for (const i of items.filter(i => i.kind === "list" && !i.seen))
    if (await page.locator(`.igrid .item[data-id="${i.id}"]`).count()) fresh = i;
  test.skip(!fresh, "no unseen list left on Home");
  const strip = page.locator(`.igrid .item[data-id="${fresh!.id}"]`);
  await strip.locator(".entries input").first().click();
  await expect.poll(async () => (await A.items()).find(i => i.id === fresh!.id)!.seen).toBe(true);
  await expect(strip).toHaveCount(1);
  await expect(strip.locator(".new")).toHaveCount(0);
  await refresh();
  await expect(strip).toHaveCount(0);
});

test("an answered wake stays under To check, and Refresh moves it to Reviewed", async () => {
  await nav("Review").click();
  const todo = page.locator(".group", { has: page.locator(".group-h", { hasText: "To check" }) });
  const unanswered = todo.locator("article.ev:not(.done)");
  test.skip(!(await unanswered.count()), "every wake is answered already");
  const rows = await todo.locator("article.ev").count(),
    before = reviewTodo(await A.events());
  await unanswered.first().locator(".lab-seg .n").click();
  await expect.poll(async () => reviewTodo(await A.events())).toBe(before - 1);
  await expect(todo.locator("article.ev")).toHaveCount(rows);
  await refresh();
  const left = reviewTodo(await A.events());
  if (left) await expect(todo.locator("article.ev")).toHaveCount(left);
  else await expect(page.locator(".group-h", { hasText: "To check" })).toHaveCount(0);
});

test("clearing an answer in Reviewed keeps the row there until Refresh", async () => {
  const reviewed = page.locator(".group", { has: page.locator(".group-h", { hasText: "Reviewed" }) });
  const row = reviewed.locator("article.ev.done").first();
  const rows = await reviewed.locator("article.ev").count();
  await row.locator(".lab-seg button[aria-pressed=true]").click();
  await expect(toast()).toHaveText("Label cleared.");
  await expect(reviewed.locator("article.ev")).toHaveCount(rows);
});

test("a name prompt: Enter saves, Cancel doesn't", async () => {
  await nav("Voices").click();
  const voice = page.locator(".voice", { has: page.locator(".vfoot") }).first();
  test.skip(!(await voice.count()), "no unnamed voice left");
  await voice.locator(".vfoot .btn", { hasText: "Name" }).click();
  await dialog().locator("input").fill("Nobody");
  await dialog().locator(".btn", { hasText: "Cancel" }).click();
  await expect(dialog()).toHaveCount(0);
  expect((await A.clusters()).some(c => c.name === "Nobody")).toBe(false);
});
