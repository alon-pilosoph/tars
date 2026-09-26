import type { Item } from "../types";
import { type Dialog, type MenuTarget, get, set } from "./core";

export const PHONE = "(max-width: 640px)"; // the design's phone breakpoint (styles.css)
export const HEADER_OFFSET = 76; // the sticky top bar, plus a little room

export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

let toastTimer = 0,
  toastN = 0;
export function toast(msg: string, undo?: () => void, ms = 5000) {
  const id = ++toastN;
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
export function closeMenu() {
  if (get().menu) set({ menu: null });
}

type Ask<D> = Omit<D, "kind" | "resolve">;
/** A confirmation. `text` may mark **names** in bold. */
export const confirm = (d: Ask<Extract<Dialog, { kind: "confirm" }>>) =>
  new Promise<boolean>(resolve => set({ dialog: { kind: "confirm", ...d, resolve } }));
/** A one-line question; null when cancelled. */
export const prompt = (d: Ask<Extract<Dialog, { kind: "prompt" }>>) =>
  new Promise<string | null>(resolve => set({ dialog: { kind: "prompt", ...d, resolve } }));
export const openNote = (item: Item) => set({ dialog: { kind: "note", item } });

export function scrollToEl(sel: string) {
  const el = document.querySelector(sel);
  if (el) scrollTo(0, el.getBoundingClientRect().top + scrollY - HEADER_OFFSET);
}

/** Copies text. The clipboard API only exists on https and localhost, and the Pi serves plain http on the home
    network, so there's a fallback. It has to run inside the click, before anything awaits. */
export async function copyText(text: string, done: string) {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return toast(done);
    } catch {
      /* try the fallback */
    }
  }
  const was = document.activeElement as HTMLElement | null,
    ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.append(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    /* not allowed */
  }
  ta.remove();
  was?.focus({ preventScroll: true });
  toast(ok ? done : "Couldn't copy from this browser.");
}
