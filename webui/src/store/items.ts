import type { MouseEvent } from "react";
import { post } from "../api";
import { itemName } from "../format";
import { plain } from "../markdown";
import { DEMO } from "../params";
import type { Item } from "../types";
import { confirmDelete, run } from "./actions";
import { get, set } from "./core";
import { itemById } from "./selectors";
import { copyText, openNote, toast } from "./ui";

const patchItem = (id: number, p: (i: Item) => Partial<Item>) =>
  set(s => ({ items: s.items.map(i => (i.id === id ? { ...i, ...p(i) } : i)) }));

export function markSeen(i: Item | undefined) {
  if (!i || i.seen) return;
  patchItem(i.id, () => ({ seen: true }));
  run(() => post(`/api/items/${i.id}/seen`));
}

/** Only web links open: a link TARS sent is checked on the server too, but the page doesn't trust it. */
export const safeUrl = (u: string | null | undefined) => (u && /^https?:\/\//i.test(u) ? u : undefined);
export const fileUrl = (i: Item) => (DEMO ? "#" : `/api/items/${i.id}/file`);
export const imageHref = (i: Item) => (DEMO ? "#" : i.preview || fileUrl(i));

/** A click on one of an item's links (open, download, the image): marks it seen once the browser has followed it. */
export function followed(e: MouseEvent<HTMLAnchorElement>, i: Item, what: "open" | "download") {
  if (DEMO && e.currentTarget.getAttribute("href") === "#") {
    e.preventDefault();
    if (what === "download") toast("Demo: no file to download.");
  }
  setTimeout(() => markSeen(itemById(get(), i.id)), 0);
}

export function openLink(i: Item | undefined) {
  const url = safeUrl(i?.url);
  if (!i || !url) return;
  window.open(url, "_blank", "noopener");
  markSeen(i);
}

export function download(i: Item | undefined) {
  if (!i) return;
  if (DEMO) toast("Demo: no file to download.");
  else Object.assign(document.createElement("a"), { href: fileUrl(i), download: i.name || "" }).click();
  markSeen(i);
}

export function showNote(i: Item | undefined) {
  if (!i) return;
  openNote(i);
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
      return i.name || "";
  }
}

export async function copyItem(i: Item | undefined) {
  if (!i) return;
  const copied = copyText(asText(i), i.kind === "link" ? "Link copied." : "Copied.");
  markSeen(itemById(get(), i.id));
  await copied;
}

export async function tick(id: number, n: number, done: boolean) {
  if (!itemById(get(), id)?.entries?.[n]) return;
  patchItem(id, x => ({ seen: true, entries: x.entries!.map((e, k) => (k === n ? { ...e, done } : e)) }));
  await run(() => post(`/api/items/${id}/entries/${n}`, { done }));
}

export const showWholeList = (id: number) => set(s => ({ lists: new Set([...s.lists, id]) }));

export async function delItem(id: number) {
  const i = itemById(get(), id);
  if (!i) return;
  await confirmDelete(
    `Delete “${itemName(i)}”?`,
    "It disappears from Sent and from its conversation, for everyone at home. This can't be undone.",
    `/api/items/${id}`,
    "Deleted.",
  );
}
