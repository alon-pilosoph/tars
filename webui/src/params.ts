/* The page's link parameters, for linking to a view:

   ?tab=home|sent|review|reminders|voices|models     ?person=all|household|<voice id>
   ?conv=<id>     that conversation, open and scrolled to
   ?item=<id>     scrolled to that item
   ?demo          built-in sample data, no server; demoParams.ts adds more parameters for the screenshot tests */

export type Tab = "home" | "sent" | "review" | "reminders" | "voices" | "models";
export type PersonFilter = "all" | "household" | number;

const TABS: Tab[] = ["home", "sent", "review", "reminders", "voices", "models"];

export const PARAMS = new URLSearchParams(location.search);
export const idParam = (v: string | null | undefined) => (v && /^\d+$/.test(v) ? Number(v) : null);
export const oneOf = <T extends string>(v: string | null | undefined, options: readonly T[]) =>
  options.includes(v as T) ? (v as T) : null;

function personParam(v: string | null): PersonFilter {
  if (v === "household") return "household";
  return idParam(v) ?? "all";
}

export const DEMO = PARAMS.has("demo");
export const LINK = {
  tab: oneOf(PARAMS.get("tab"), TABS) ?? "home",
  person: personParam(PARAMS.get("person")),
  conv: idParam(PARAMS.get("conv")),
  item: idParam(PARAMS.get("item")),
};

export function linkTo(q: Record<string, string | number> = {}) {
  const p = new URLSearchParams();
  if (DEMO) p.set("demo", PARAMS.get("demo") || "");
  for (const [k, v] of Object.entries(q)) p.set(k, String(v));
  return "?" + p.toString().replace(/^demo=(&|$)/, "demo$1");
}
