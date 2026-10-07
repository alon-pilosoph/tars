import { post } from "../api";
import { stopAudio } from "../audio";
import { itemName } from "../format";
import { plain } from "../markdown";
import type { Item } from "../types";
import { confirmDelete, run } from "./actions";
import { get, set, setNow } from "./core";
import { itemById } from "./selectors";
import { copyText, openItem, scrollToEl } from "./ui";

const patchItem = (id: number, patch: (i: Item) => Partial<Item>) =>
  set(s => ({ items: s.items.map(i => (i.id === id ? { ...i, ...patch(i) } : i)) }));

export function markSeen(i: Item | undefined) {
  if (!i || i.seen) return;
  patchItem(i.id, () => ({ seen: true }));
  run(() => post(`/api/items/${i.id}/seen`), { revert: () => patchItem(i.id, () => ({ seen: false })) });
}

/** Only http(s) links open. The server checks links TARS sends too; the page doesn't rely on that. */
export const safeUrl = (u: string | null | undefined) => (u && /^https?:\/\//i.test(u) ? u : undefined);

/** For a click on an item's own link: marks it seen once the browser has followed the click. */
export function followed(i: Item) {
  setTimeout(() => markSeen(itemById(get(), i.id)), 0);
}

export function openLink(i: Item | undefined) {
  const url = safeUrl(i?.url);
  if (!i || !url) return;
  window.open(url, "_blank", "noopener,noreferrer");
  markSeen(i);
}

export function download(i: Item | undefined) {
  if (!i?.url) return;
  Object.assign(document.createElement("a"), { href: i.url, download: i.name || "" }).click();
  markSeen(i);
}

export function showItem(i: Item | undefined) {
  if (!i) return;
  openItem(i.kind === "note" ? "note" : "image", i);
  markSeen(i);
}

function asText(i: Item) {
  switch (i.kind) {
    case "link":
      return i.url || "";
    case "note":
      return `${i.title}\n\n${plain(i.body || "")}`;
    case "list":
      return `${i.title}\n${(i.entries || []).map(e => `${e.done ? "[x]" : "[ ]"} ${e.text}`).join("\n")}`;
    case "file":
      return i.url ? new URL(i.url, location.href).href : i.name || "";
  }
}

export async function copyItem(i: Item | undefined) {
  if (!i) return;
  const copied = copyText(asText(i), i.kind === "link" ? "Link copied." : "Copied.");
  markSeen(itemById(get(), i.id));
  await copied;
}

export async function tick(id: number, index: number, done: boolean) {
  const item = itemById(get(), id);
  if (!item?.entries?.[index]) return;
  const show = (value: boolean, seen: boolean) =>
    patchItem(id, i => ({
      seen,
      entries: i.entries?.map((e, k) => (k === index ? { ...e, done: value } : e)),
    }));
  show(done, true); // the server marks it seen too
  await run(() => post(`/api/items/${id}/entries/${index}`, { done }), { revert: () => show(!done, item.seen) });
}

export function toggleWholeList(id: number) {
  set(s => {
    const lists = new Set(s.lists);
    if (!lists.delete(id)) lists.add(id);
    return { lists };
  });
}

export function goItem(id: number) {
  stopAudio();
  setNow(s => ({ tab: "sent", person: "all", lists: new Set([...s.lists, id]), menu: null }));
  scrollToEl(`.item[data-id="${id}"]`);
  markSeen(itemById(get(), id));
}

export async function delItem(id: number) {
  const i = itemById(get(), id);
  if (!i) return;
  await confirmDelete(
    `Delete “${itemName(i)}”?`,
    "It goes from Sent for everyone at home. This can't be undone.",
    `/api/items/${id}`,
    "Deleted.",
  );
}
