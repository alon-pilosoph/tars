/* App state: one store the components read with useStore(); actions update it immutably. */
import { useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { LINK, type PersonFilter, type Tab } from "../params";
import type { Cluster, Conversation, Item, ModelsInfo, Status, TarsEvent } from "../types";

export interface Toast {
  id: number;
  msg: string;
  undo?: () => void;
}

export type DialogState =
  | { kind: "confirm"; title: string; text?: string; ok?: string; danger?: boolean; resolve: (ok: boolean) => void }
  | {
      kind: "prompt";
      title: string;
      text?: string;
      placeholder: string;
      value?: string;
      ok?: string;
      required?: boolean;
      maxLength?: number;
      resolve: (value: string | null) => void;
    }
  | { kind: "note"; item: Item }
  | { kind: "image"; item: Item };

export type ItemPlace = "thread" | "home" | "sent";

export type MenuTarget =
  | { kind: "event" | "eventVoice" | "voice" | "conversation"; id: number }
  | { kind: "item"; id: number; place: ItemPlace } // the same item can be on the page twice
  | { kind: "more" };
export type MenuState = MenuTarget & { anchor: HTMLElement; sheet: boolean };

/** What the page showed at the last Refresh or page load. Reloads after your own actions only update these, so
    nothing appears or moves by itself and a card never vanishes from under your finger. */
export interface Snapshot {
  events: Set<number>;
  convs: Set<number>;
  turns: Set<number>;
  items: Set<number>;
  newItems: Set<number>;
  review: Set<number>;
}

export interface State {
  tab: Tab;
  person: PersonFilter;
  phase: "loading" | "ready" | "error";
  error: string;
  events: TarsEvent[];
  clusters: Cluster[];
  models: ModelsInfo | null;
  status: Status;
  convs: Conversation[];
  items: Item[];
  snapshot: Snapshot;
  open: Set<number>; // conversations shown with every turn
  lists: Set<number>; // lists shown with every entry
  edit: number | null; // the turn whose transcript is being corrected
  refreshing: boolean;
  reclustering: boolean;
  toast: Toast | null;
  dialog: DialogState | null;
  menu: MenuState | null;
}

let current: State = {
  tab: LINK.tab,
  person: LINK.person,
  phase: "loading",
  error: "",
  events: [],
  clusters: [],
  models: null,
  status: { clustering: false, enroll_at: 0 },
  convs: [],
  items: [],
  snapshot: {
    events: new Set(),
    convs: new Set(),
    turns: new Set(),
    items: new Set(),
    newItems: new Set(),
    review: new Set(),
  },
  open: new Set(LINK.conv != null ? [LINK.conv] : []),
  lists: new Set(),
  edit: null,
  refreshing: false,
  reclustering: false,
  toast: null,
  dialog: null,
  menu: null,
};
const subs = new Set<() => void>();
type Patch = Partial<State> | ((s: State) => Partial<State>);

export const get = () => current;
export function set(patch: Patch) {
  current = { ...current, ...(typeof patch === "function" ? patch(current) : patch) };
  subs.forEach(f => f());
}
/** Renders before returning, so the caller can scroll to or measure what changed. */
export const setNow = (patch: Patch) => flushSync(() => set(patch));
function subscribe(f: () => void) {
  subs.add(f);
  return () => {
    subs.delete(f);
  };
}
export const useStore = () => useSyncExternalStore(subscribe, get);
