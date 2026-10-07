import type { PersonFilter } from "../params";
import type { Cluster, Conversation, Item, Reminder, Speaker, Turn } from "../types";
import type { MenuTarget, State } from "./core";

export const eventById = (s: State, id: number) => s.events.find(e => e.id === id);
export const clusterById = (s: State, id: number) => s.clusters.find(c => c.id === id);
export const itemById = (s: State, id: number) => s.items.find(i => i.id === id);
export const convById = (s: State, id: number | null | undefined) => s.convs.find(c => c.id === id);
export function findTurn(s: State, id: number): [Turn, Conversation] | [] {
  for (const c of s.convs) for (const t of c.turns) if (t.id === id) return [t, c];
  return [];
}
export const turnItems = (s: State, t: Turn) =>
  (t.items || []).map(id => itemById(s, id)).filter((i): i is Item => !!i);

export const voiceName = (c: Pick<Cluster, "id" | "name"> & { kind?: Cluster["kind"] }) =>
  c.name || (c.kind === "not_person" ? "Not a person" : `Voice ${c.id}`);

/** Scheduled or waiting for a got it: what TARS will still say. */
export const isActiveReminder = (r: Reminder) => r.status === "scheduled" || r.status === "waiting";
/** Said and waiting for someone to say they got it: the Reminders tab's count. */
export const remindersWaiting = (s: State) => s.reminders?.reminders.filter(r => r.status === "waiting") ?? [];
export function clusterName(s: State, id: number | null | undefined) {
  if (id == null) return null;
  return voiceName(clusterById(s, id) ?? { id, name: null });
}
export const speakerName = (s: State, sp: Speaker | null | undefined) =>
  sp ? sp.name || clusterName(s, sp.cluster_id) || "Unknown voice" : null;

export const people = (s: State) =>
  s.clusters.filter((c): c is Cluster & { name: string } => !!c.name && c.kind === "person");
export const unnamedVoices = (s: State) => s.clusters.filter(c => !c.name && c.kind !== "not_person");
export const notPeople = (s: State) => s.clusters.filter(c => c.kind === "not_person");

/** A wake that a request followed labels itself; only the rest need a look. */
export const reviewEvents = (s: State) => s.events.filter(e => e.follow !== "asked");
export const reviewTodo = (s: State) => reviewEvents(s).filter(e => !e.label);
export const unseen = (s: State) => s.items.filter(i => !i.seen);

/** The filter carries across tabs, but a page only applies one its control can show: Home has no Household option
    and shows the control only with two or more people. */
export function personShown(s: State, withHousehold: boolean): PersonFilter {
  const p = s.person;
  const named = people(s);
  if (p === "all") return p;
  if (p === "household") return withHousehold ? p : "all";
  return named.some(c => c.id === p) && (withHousehold || named.length > 1) ? p : "all";
}
export const forPerson = (i: Item, p: PersonFilter) =>
  p === "all" || (p === "household" ? i.scope === "household" : i.scope !== "household" && i.for?.cluster_id === p);
export const convPerson = (c: Conversation, p: PersonFilter) =>
  p === "all" || p === "household" || c.speaker?.cluster_id === p;

export function forLabel(s: State, i: Item) {
  if (i.scope === "household") return "For the household";
  return `For ${i.for?.name || clusterName(s, i.for?.cluster_id) || "whoever asked"}`;
}

const menuKey = (t: MenuTarget) => [t.kind, "id" in t ? t.id : ""].join(":");
export const menuOpen = (s: State, target: MenuTarget) => !!s.menu && menuKey(s.menu) === menuKey(target);
