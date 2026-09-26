/* Loading. refresh() is the page load and the Refresh button: everything as it is now, with finished things moved
   to where they belong. reload() runs after your own actions and stays within what the last Refresh showed. */
import { get as fetchJson, writesDone } from "../api";
import type { ApiConversation, Cluster, Conversation, Item, Models, RetrainResult, Status, TarsEvent } from "../types";
import { type Snapshot, type State, get, set, setNow } from "./core";
import { errText, toast } from "./ui";

interface Data {
  events: TarsEvent[];
  clusters: Cluster[];
  models: Models;
  status: Status;
  convs: Conversation[];
  items: Item[];
}

/** The API puts sent items inside the turns that sent them; the page keeps each item once, and ids in the turns. */
export function normalize(apiConvs: ApiConversation[], apiItems: Item[]) {
  const items = new Map(apiItems.map(i => [i.id, i]));
  const convs = apiConvs.map(c => ({
    ...c,
    turns: c.turns.map(t => ({
      ...t,
      items: t.items?.map(x => {
        if (typeof x === "number") return x;
        if (!items.has(x.id)) items.set(x.id, x);
        return x.id;
      }),
    })),
  }));
  return { convs, items: [...items.values()] };
}

export function snapshotOf(d: Pick<Data, "events" | "convs" | "items">): Snapshot {
  return {
    events: new Set(d.events.map(e => e.id)),
    convs: new Set(d.convs.map(c => c.id)),
    turns: new Set(d.convs.flatMap(c => c.turns.map(t => t.id))),
    items: new Set(d.items.map(i => i.id)),
    newItems: new Set(d.items.filter(i => !i.seen).map(i => i.id)),
    review: new Set(d.events.filter(e => e.follow !== "asked" && !e.label).map(e => e.id)),
  };
}

/** Only what the snapshot showed, as it is now. */
export function within<D extends Pick<Data, "events" | "convs" | "items">>(snap: Snapshot, d: D): D {
  return {
    ...d,
    events: d.events.filter(e => snap.events.has(e.id)),
    convs: d.convs
      .filter(c => snap.convs.has(c.id))
      .map(c => ({ ...c, turns: c.turns.filter(t => snap.turns.has(t.id)) })),
    items: d.items.filter(i => snap.items.has(i.id)),
  };
}

/** While a retrain runs, the page shows its own result; otherwise the server's. */
function retrainShown(s: State, models: Models): RetrainResult | null | undefined {
  if (s.retraining && s.lastRetrain !== undefined) return s.lastRetrain;
  return models.last_retrain ?? s.lastRetrain ?? null;
}

async function fetchAll(fresh: boolean) {
  try {
    await writesDone();
    const [events, clusters, models, status, convs, items] = await Promise.all([
      fetchJson<TarsEvent[]>("/api/events"),
      fetchJson<Cluster[]>("/api/clusters"),
      fetchJson<Models>("/api/models"),
      fetchJson<Status>("/api/status"),
      fetchJson<ApiConversation[]>("/api/conversations"),
      fetchJson<Item[]>("/api/items"),
    ]);
    let data: Data = { events, clusters, models, status, ...normalize(convs, items) };
    const snapshot = fresh ? snapshotOf(data) : get().snapshot;
    if (!fresh) data = within(snapshot, data);
    setNow(s => ({ ...data, snapshot, phase: "ready", lastRetrain: retrainShown(s, models) }));
  } catch (e) {
    // A failed reload after an action keeps the page as it is; only a failed page load or Refresh replaces it.
    if (fresh || get().phase !== "ready") set({ phase: "error", error: errText(e) || "Failed to fetch" });
    else toast(`Couldn't reload (${errText(e)}).`);
  }
}

// One load at a time, in order, so an older answer never lands on top of a newer one.
let loads: Promise<void> = Promise.resolve();
function queue(fresh: boolean) {
  loads = loads.then(() => fetchAll(fresh));
  return loads;
}
export const refresh = () => queue(true);
export const reload = () => queue(false);

export async function refreshNow() {
  set({ refreshing: true });
  await refresh();
  set({ refreshing: false });
}

export function retry() {
  set({ phase: "loading" });
  refresh();
}
