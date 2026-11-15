import { heardText } from "../format";
import type { TarsEvent } from "../types";

/** What a wake or near-miss was, in a line: Review's rows and its Listen. */
export function What({ e }: { e: TarsEvent }) {
  if (e.kind === "near_miss") return <>Almost woke, then didn't</>;
  const h = <span className="heard">{`“${heardText(e.heard || "")}”`}</span>;
  if (e.outcome === "ignore") return <>Heard {h} and stayed quiet</>;
  if (e.outcome === "ask") return <>Heard {h} and asked “Did you call me?”</>;
  if (e.follow === "said_nothing") return <>Woke on {h}, but nobody spoke</>;
  if (e.follow === "not_for_us") return <>Woke on {h} and heard this</>;
  return <>Woke on {h}</>;
}
