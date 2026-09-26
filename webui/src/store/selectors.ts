import type { PersonFilter } from "../params";
import type { Cluster, Conversation, Item, Speaker, Turn } from "../types";
import type { MenuTarget, State } from "./core";

export const voiceName = (c: Pick<Cluster, "id" | "name">) => c.name || `Voice ${c.id}`;
export function cName(s: State, id: number | null | undefined) {
  if (id == null) return null;
  return voiceName(s.clusters.find(c => c.id === id) ?? { id, name: null });
}
export const speakerName = (s: State, sp: Speaker | null | undefined) =>
  sp ? sp.name || cName(s, sp.cluster_id) || "Unknown voice" : null;

export const people = (s: State) =>
  s.clusters.filter((c): c is Cluster & { name: string } => !!c.name && c.kind !== "not_person");
export const unnamedVoices = (s: State) => s.clusters.filter(c => !c.name && c.kind !== "not_person");
export const notPeople = (s: State) => s.clusters.filter(c => c.kind === "not_person");

/** Wakes a conversation followed label themselves; the rest need a look. */
export const reviewEvents = (s: State) => s.events.filter(e => e.follow !== "asked");
export const reviewTodo = (s: State) => reviewEvents(s).filter(e => !e.label);
export const unseen = (s: State) => s.items.filter(i => !i.seen);

export const itemById = (s: State, id: number) => s.items.find(i => i.id === id);
export const turnItems = (s: State, t: Turn) =>
  (t.items || []).map(id => itemById(s, id)).filter((i): i is Item => !!i);
export const convById = (s: State, id: number | null | undefined) => s.convs.find(c => c.id === id);
export function findTurn(s: State, id: number): [Turn, Conversation] | [] {
  for (const c of s.convs) for (const t of c.turns) if (t.id === id) return [t, c];
  return [];
}

/** The person filter a page shows. The filter carries across tabs, but a page only applies one it has a control
    for: Home has no Household option and only shows the control with two or more people; a voice that's no longer
    a named person matches nothing. */
export function personShown(s: State, withHousehold: boolean): PersonFilter {
  const p = s.person,
    ps = people(s);
  if (p === "all") return p;
  if (p === "household") return withHousehold ? p : "all";
  return ps.some(c => c.id === p) && (withHousehold || ps.length > 1) ? p : "all";
}
export const forPerson = (i: Item, p: PersonFilter) =>
  p === "all" || (p === "household" ? i.scope === "household" : i.scope !== "household" && i.for?.cluster_id === p);
export const convPerson = (c: Conversation, p: PersonFilter) =>
  p === "all" || p === "household" || c.speaker?.cluster_id === p;

export const forLabel = (s: State, i: Item) =>
  i.scope === "household"
    ? "For the household"
    : `For ${i.for?.name || (i.for?.cluster_id != null ? cName(s, i.for.cluster_id) : "whoever asked")}`;

export function menuOpen(s: State, kind: MenuTarget["kind"], id?: number) {
  const m = s.menu;
  return !!m && m.kind === kind && (!("id" in m) || m.id === id);
}
