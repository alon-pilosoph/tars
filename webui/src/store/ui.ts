import type { Item } from "../types";
import { type DialogState, type MenuTarget, get, set } from "./core";

const PHONE = "(max-width: 720px)"; // must match the phone breakpoint in styles.css
const HEADER_OFFSET = 84;

export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

let toastTimer = 0;
let toastCount = 0;
export function toast(msg: string, undo?: () => void, ms = 5000) {
  const id = ++toastCount;
  set({ toast: { id, msg, undo } });
  clearTimeout(toastTimer);
  if (ms)
    toastTimer = window.setTimeout(() => {
      if (get().toast?.id === id) closeToast();
    }, ms);
}
export const closeToast = () => set({ toast: null });

export function toggleMenu(target: MenuTarget, anchor: HTMLElement) {
  if (get().menu?.anchor === anchor) return closeMenu();
  set({ menu: { ...target, anchor, sheet: matchMedia(PHONE).matches } });
}
/** `refocus` puts focus back on the ⋯ button, so a dialog the menu opens returns focus there too. */
export function closeMenu(refocus = false) {
  const m = get().menu;
  if (!m) return;
  set({ menu: null });
  if (refocus && m.anchor.isConnected) m.anchor.focus({ preventScroll: true });
}

type Ask<D> = Omit<D, "kind" | "resolve">;
/** `text` may mark **names** in bold. */
export const confirm = (d: Ask<Extract<DialogState, { kind: "confirm" }>>) =>
  new Promise<boolean>(resolve => set({ dialog: { kind: "confirm", ...d, resolve } }));
/** Null when cancelled. */
export const prompt = (d: Ask<Extract<DialogState, { kind: "prompt" }>>) =>
  new Promise<string | null>(resolve => set({ dialog: { kind: "prompt", ...d, resolve } }));
export const openItem = (kind: "note" | "image", item: Item) => set({ dialog: { kind, item } });

export function scrollToEl(selector: string) {
  const el = document.querySelector(selector);
  if (!el) return;
  scrollTo(0, el.getBoundingClientRect().top + scrollY - HEADER_OFFSET);
  document.querySelectorAll(".hit").forEach(x => x.classList.remove("hit"));
  el.classList.add("hit");
  addEventListener("pointerdown", () => el.classList.remove("hit"), { once: true });
}

/** Copies text. The clipboard API only exists on https and localhost, and the Pi serves plain http on the home
    network, so there's a fallback. It has to run inside the click, before anything awaits, and inside an open
    modal dialog when there is one: everything outside it is inert, so nothing there can be selected. */
export async function copyText(text: string, done: string) {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return toast(done);
    } catch {
      /* try the fallback */
    }
  }
  const was = document.activeElement;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  (document.querySelector("dialog[open]") ?? document.body).append(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    /* not allowed */
  }
  area.remove();
  if (was instanceof HTMLElement) was.focus({ preventScroll: true });
  toast(ok ? done : "Couldn't copy from this browser.");
}
