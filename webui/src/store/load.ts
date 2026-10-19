/* Loading. refresh() is the page load and the Refresh button: everything as it is now, with finished things moved
   to where they belong. reload() runs after your own actions and stays within what the last Refresh showed. */
import { getJson, writesDone, writesMade } from "../api";
import type {
  ApiConversation,
  Cluster,
  Conversation,
  Item,
  ModelsInfo,
  RemindersInfo,
  Status,
  TarsEvent,
} from "../types";
import { type LoadPart, type Snapshot, type State, get, set, setNow } from "./core";
import { healthLoaded, loadHealth } from "./health";
import { errText, toast } from "./ui";

interface Data {
  events: TarsEvent[];
  clusters: Cluster[];
  models: ModelsInfo | null;
  status: Status;
  convs: Conversation[];
  items: Item[];
  reminders: RemindersInfo | null;
}

type Answers = Omit<Data, "convs"> & { convs: ApiConversation[] };
type Settled<T> = { [K in keyof T]: PromiseSettledResult<T[K]> };

/** Each part as its endpoint answered it, or, where that failed, as it last loaded, with why it failed. Only when
    every endpoint failed is the page unreachable: then the first one's error is thrown. */
export function settle<T extends Record<LoadPart, unknown>>(
  answers: Settled<T>,
  last: T,
): { data: T; failed: Partial<Record<LoadPart, string>> } {
  const data = {} as T;
  const failed: Partial<Record<LoadPart, string>> = {};
  let error: unknown = null;
  for (const part of Object.keys(answers) as LoadPart[]) {
    const answer = answers[part];
    if (answer.status === "fulfilled") data[part] = answer.value;
    else {
      data[part] = last[part];
      failed[part] = errText(answer.reason) || "Failed to fetch";
      error ??= answer.reason;
    }
  }
  if (Object.keys(failed).length === Object.keys(answers).length) throw error;
  return { data, failed };
}

/** What each part is, as the page names it, and the pages that show it (null: every page). */
export const LOAD_PARTS: Record<LoadPart, { what: string; tabs: State["tab"][] | null }> = {
  events: { what: "the wakes", tabs: ["review"] },
  clusters: { what: "the voices", tabs: ["voices"] },
  models: { what: "the models", tabs: ["models"] },
  status: { what: "what's set up", tabs: null },
  convs: { what: "the conversations", tabs: ["home"] },
  items: { what: "what TARS sent", tabs: ["sent"] },
  reminders: { what: "the reminders", tabs: ["reminders"] },
};

/** The parts the page in view shows that didn't load with the last Refresh or reload, with why. */
export const failedHere = (s: Pick<State, "tab" | "loadErrors">) =>
  (Object.entries(s.loadErrors) as [LoadPart, string][])
    .filter(([part]) => LOAD_PARTS[part].tabs?.includes(s.tab) ?? true)
    .map(([part, why]) => ({ part, what: LOAD_PARTS[part].what, why }));

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

/** Every part at once; one endpoint failing keeps that part as it last loaded, and says so on its page. */
async function fetchData(): Promise<{ data: Data; failed: Partial<Record<LoadPart, string>> }> {
  const [events, clusters, models, status, convs, items, reminders] = await Promise.allSettled([
    getJson<TarsEvent[]>("/api/events"),
    getJson<Cluster[]>("/api/clusters"),
    getJson<ModelsInfo>("/api/models"),
    getJson<Status>("/api/status"),
    getJson<ApiConversation[]>("/api/conversations"),
    getJson<Item[]>("/api/items"),
    getJson<RemindersInfo>("/api/reminders"),
  ]);
  const { data, failed } = settle<Answers>({ events, clusters, models, status, convs, items, reminders }, get());
  return { data: { ...data, ...normalize(data.convs, data.items) }, failed };
}

/** True if it worked. `quiet`: a failed reload shows no toast, because the caller already showed one. */
async function fetchAll(fresh: boolean, quiet: boolean) {
  try {
    await writesDone();
    let loaded: Awaited<ReturnType<typeof fetchData>>;
    let writes;
    // A change made while this loaded may have reached the server after it answered: load again, or the page
    // would show it undone.
    do {
      writes = writesMade();
      loaded = await fetchData();
      await writesDone();
    } while (writes !== writesMade());
    const snapshot = fresh ? snapshotOf(loaded.data) : get().snapshot;
    const data = fresh ? loaded.data : within(snapshot, loaded.data);
    setNow({ ...data, snapshot, phase: "ready", loadErrors: loaded.failed });
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

/** Once every change made so far has reached the server and every load it started has landed. The screenshot
    tests wait for it, so a picture is never taken halfway through an action. */
export async function settled() {
  let writes;
  do {
    writes = writesMade();
    await writesDone();
    await loads;
    await healthLoaded();
  } while (writes !== writesMade());
}

export async function refreshNow() {
  set({ refreshing: true });
  await Promise.all([refresh(), get().tab === "status" ? loadHealth() : null]);
  set({ refreshing: false });
}

export function retry() {
  set({ phase: "loading" });
  refresh();
}
