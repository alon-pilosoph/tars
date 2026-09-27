/* App state: one store the components read with useStore(); actions update it immutably. */
import { useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { LINK, type PersonFilter, type Tab } from "../params";
import type { Cluster, Conversation, Item, Models, Status, TarsEvent } from "../types";

export interface Toast {
  id: number;
  msg: string;
  undo?: () => void;
}

export type Dialog =
  | { kind: "confirm"; title: string; text?: string; ok?: string; danger?: boolean; resolve: (ok: boolean) => void }
  | {
      kind: "prompt";
      title: string;
      text?: string;
      placeholder: string;
      value?: string;
      ok?: string;
      resolve: (value: string | null) => void;
    }
  | { kind: "note"; item: Item };

export type MenuKind = "event" | "eventVoice" | "voiceCard" | "item" | "conversation";
export type MenuTarget = { kind: MenuKind; id: number } | { kind: "more" };
export type MenuState = MenuTarget & { anchor: HTMLElement; sheet: boolean };

/** What the page showed at the last Refresh (or page load). Nothing appears or moves by itself: loads in between,
    after your own actions, update only these, so a card never vanishes from under your finger. */
export interface Snapshot {
  events: Set<number>;
  convs: Set<number>;
  turns: Set<number>;
  items: Set<number>;
  newItems: Set<number>; // shown as new: Home's strip, Sent's "New"
  review: Set<number>; // wakes shown under "To check"
}

export interface State {
  tab: Tab;
  person: PersonFilter;
  phase: "loading" | "ready" | "error";
  error: string;
  events: TarsEvent[];
  clusters: Cluster[];
  models: Models | null;
  status: Status | null;
  convs: Conversation[];
  items: Item[];
  snapshot: Snapshot;
  open: Set<number>; // conversations shown with every turn
  lists: Set<number>; // lists shown with every entry
  edit: number | null; // the turn whose transcript is being corrected
  refreshing: boolean;
  reclustering: boolean;
  toast: Toast | null;
  dialog: Dialog | null;
  menu: MenuState | null;
}

export const emptySnapshot = (): Snapshot => ({
  events: new Set(),
  convs: new Set(),
  turns: new Set(),
  items: new Set(),
  newItems: new Set(),
  review: new Set(),
});

let S: State = {
  tab: LINK.tab,
  person: LINK.person,
  phase: "loading",
  error: "",
  events: [],
  clusters: [],
  models: null,
  status: null,
  convs: [],
  items: [],
  snapshot: emptySnapshot(),
  open: new Set(LINK.conv != null ? [LINK.conv] : []),
  lists: new Set(),
  edit: LINK.edit,
  refreshing: false,
  reclustering: false,
  toast: null,
  dialog: null,
  menu: null,
};
const subs = new Set<() => void>();
type Patch = Partial<State> | ((s: State) => Partial<State>);

export const get = () => S;
export function set(patch: Patch) {
  S = { ...S, ...(typeof patch === "function" ? patch(S) : patch) };
  subs.forEach(f => f());
}
/** Set, and have the page show it before returning (to scroll to or measure what changed). */
export const setNow = (patch: Patch) => flushSync(() => set(patch));
function subscribe(f: () => void) {
  subs.add(f);
  return () => {
    subs.delete(f);
  };
}
export const useStore = () => useSyncExternalStore(subscribe, get);
