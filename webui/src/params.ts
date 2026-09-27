/* The page's link parameters. Most open a state for screenshots and the checks; all of them work with ?demo.

   ?demo                  built-in sample data, no server (?demo=empty: none; ?demo=long: everything long)
   ?tab=home|sent|review|voices|models     ?person=all|household|<voice id>     ?theme=light|dark
   ?state=loading|error   ?seen=all (demo: everything seen)     ?review=done (demo: every wake answered)
   ?conv=<id>             that conversation, open and scrolled to     ?edit=<turn id>: correcting it
   ?item=<id>             scrolled to that item     ?more=1: the phone's More menu open
   ?playing=<event id>-wake|<event id>-request|turn-<turn id>      a clip shown as playing
   ?modal=delete-conv[:id]|delete-item[:id]|note[:id]|delete[:id]|newvoice|merge|rollback
   ?toast=recluster|renamed     ?recluster=running */

export type Tab = "home" | "sent" | "review" | "voices" | "models";
export const TABS: Tab[] = ["home", "sent", "review", "voices", "models"];
export type PersonFilter = "all" | "household" | number;

const P = new URLSearchParams(location.search);
const id = (v: string | null | undefined) => (v && /^\d+$/.test(v) ? Number(v) : null);
const oneOf = <T extends string>(v: string | null, options: readonly T[]) =>
  options.includes(v as T) ? (v as T) : null;
const [modal, modalId] = (P.get("modal") || "").split(":");

export const DEMO = P.has("demo");
export const LINK = {
  demo: P.get("demo") || "",
  tab: oneOf(P.get("tab"), TABS) ?? "home",
  person: (P.get("person") === "household" ? "household" : (id(P.get("person")) ?? "all")) as PersonFilter,
  theme: oneOf(P.get("theme"), ["light", "dark"] as const),
  state: oneOf(P.get("state"), ["loading", "error"] as const),
  seenAll: P.get("seen") === "all",
  reviewDone: P.get("review") === "done",
  conv: id(P.get("conv")),
  edit: id(P.get("edit")),
  item: id(P.get("item")),
  more: P.get("more") === "1",
  playing: P.get("playing"),
  modal: oneOf(modal, ["delete-conv", "delete-item", "note", "delete", "newvoice", "merge", "rollback"] as const),
  modalId: id(modalId),
  toast: oneOf(P.get("toast"), ["recluster", "renamed"] as const),
  reclustering: P.get("recluster") === "running",
};
