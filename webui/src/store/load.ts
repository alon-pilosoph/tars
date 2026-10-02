/* Loading. refresh() is the page load and the Refresh button: everything as it is now, with finished things moved
   to where they belong. reload() runs after your own actions and stays within what the last Refresh showed. */
import { getJson, writesDone, writesMade } from "../api";
import type { ApiConversation, Cluster, Conversation, Item, ModelsInfo, Status, TarsEvent } from "../types";
import { type Snapshot, get, set, setNow } from "./core";
import { errText, toast } from "./ui";

interface Data {
  events: TarsEvent[];
  clusters: Cluster[];
  models: ModelsInfo;
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

async function fetchData(): Promise<Data> {
  const [events, clusters, models, status, convs, items] = await Promise.all([
    getJson<TarsEvent[]>("/api/events"),
    getJson<Cluster[]>("/api/clusters"),
    getJson<ModelsInfo>("/api/models"),
    getJson<Status>("/api/status"),
    getJson<ApiConversation[]>("/api/conversations"),
    getJson<Item[]>("/api/items"),
  ]);
  return { events, clusters, models, status, ...normalize(convs, items) };
}

/** True if it worked. `quiet`: a failed reload shows no toast, because the caller already showed one. */
async function fetchAll(fresh: boolean, quiet: boolean) {
  try {
    await writesDone();
    let loaded: Data;
    let writes;
    // A change made while this loaded may have reached the server after it answered: load again, or the page
    // would show it undone.
    do {
      writes = writesMade();
      loaded = await fetchData();
      await writesDone();
    } while (writes !== writesMade());
    const snapshot = fresh ? snapshotOf(loaded) : get().snapshot;
    const data = fresh ? loaded : within(snapshot, loaded);
    setNow({ ...data, snapshot, phase: "ready" });
    return true;
  } catch (e) {
    if (fresh || get().phase !== "ready") set({ phase: "error", error: errText(e) || "Failed to fetch" });
    else if (!quiet) toast(`Couldn't reload (${errText(e)}).`);
    return false;
  }
}

// One load at a time, in order, so an older answer never lands on top of a newer one.
let loads: Promise<boolean> = Promise.resolve(true);
function queue(fresh: boolean, quiet = false) {
  loads = loads.then(() => fetchAll(fresh, quiet));
  return loads;
}
export const refresh = () => queue(true);
export const reload = (quiet = false) => queue(false, quiet);

export async function refreshNow() {
  set({ refreshing: true });
  await refresh();
  set({ refreshing: false });
}

export function retry() {
  set({ phase: "loading" });
  refresh();
}
