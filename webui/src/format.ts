import type { AnsweredBy, FailedAt, Item, Metric, TarsEvent, Timings, Turn } from "./types";

export type Kind = "answer" | "ask" | "ignore" | "near_miss";

export const KIND_LABEL: Record<Kind, string> = {
  answer: "Answered",
  ask: "Asked",
  ignore: "Ignored",
  near_miss: "Near-miss",
};
export const ITEM_KIND_LABEL = { link: "Link", note: "Note", list: "List", file: "File" } as const;
export const COPY_LABEL = { link: "Copy link", note: "Copy text", list: "Copy list" } as const;

/** The server's version name for the models TARS was installed with. */
export const INSTALLED = "installed";
export const versionName = (v: string) => (v === INSTALLED ? "the installed models" : v);

export const kindOf = (e: TarsEvent): Kind => (e.kind === "near_miss" ? "near_miss" : (e.outcome ?? "answer"));
/** The double-check writes a word it didn't know as "[unk]". */
export const heardText = (s: string) => s.replace(/\[unk\]/g, "…");
export const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
const date = (ts: number) => new Date(ts * 1000);
export const isToday = (ts: number) => sameDay(date(ts), new Date());
const isYesterday = (ts: number) => sameDay(date(ts), new Date(Date.now() - 864e5));
export const time = (ts: number) => date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

export function day(ts: number) {
  if (isToday(ts)) return "Today";
  if (isYesterday(ts)) return "Yesterday";
  return date(ts).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
}

export function shortDay(ts: number) {
  if (isToday(ts) || isYesterday(ts)) return day(ts);
  return date(ts).toLocaleDateString([], { weekday: "long" });
}

export const stamp = (ts: number) => `${day(ts)}, ${time(ts)}`;

export function shortStamp(ts: number) {
  const dayPart =
    isToday(ts) || isYesterday(ts)
      ? day(ts)
      : date(ts).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  return `${dayPart}, ${time(ts)}`;
}

export function when(ts: number) {
  const dayPart = isToday(ts) ? "today" : isYesterday(ts) ? "yesterday" : `on ${day(ts)}`;
  return `${dayPart} at ${time(ts)}`;
}

/** Capitalized, with a full stop unless it already ends one (also inside a closing quote). */
export const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1) + (/[.?!]['"”’]?$/.test(s) ? "" : ".");

export function fmtSize(bytes: number | null | undefined) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

export const itemName = (i: Item) => i.title || i.name || "";

const ANSWERED_BY: Record<AnsweredBy, string> = {
  quick: "Qwen",
  look_up: "OpenAI, handed over",
  fallback: "OpenAI, as backup",
  openai: "OpenAI",
};
const STAGE: Record<keyof Timings, string> = {
  end_of_speech: "waited",
  stt: "speech to text",
  llm: "first sentence",
  tts: "first audio",
  total: "to first sound",
};
const FAILED_AT: Record<FailedAt, string> = {
  stt: "turning speech into text",
  llm: "writing the answer",
  tts: "speaking the answer",
  other: "answering",
};
const secs = (s: number) => `${s.toFixed(1)} s`;

/** A TARS turn's response time and who wrote it, e.g. "1.4 s · Qwen"; "" if neither was kept. */
export function answerMeta(t: Turn) {
  const total = t.timings?.total;
  return [total != null ? secs(total) : "", t.answered_by ? (ANSWERED_BY[t.answered_by] ?? t.answered_by) : ""]
    .filter(Boolean)
    .join(" · ");
}

/** Every stage's time, for a tooltip: "waited 0.25 s, speech to text 0.10 s, …". */
export function timingsDetail(timings: Timings | null | undefined) {
  if (!timings) return "";
  return (Object.keys(STAGE) as (keyof Timings)[])
    .filter(k => timings[k] != null)
    .map(k => `${STAGE[k]} ${timings[k]!.toFixed(2)} s`)
    .join(", ");
}

export const failedLine = (t: Turn) => (t.failed_at ? `Failed while ${FAILED_AT[t.failed_at] ?? FAILED_AT.other}` : "");

export type Verdict = "better" | "same" | "worse";

export function metricChange(m: Metric): { diff: number; verdict: Verdict } | null {
  const before = parseFloat(m.current);
  const after = parseFloat(m.candidate);
  if (isNaN(before) || isNaN(after)) return null;
  const diff = after - before;
  const better = m.lower_is_better ? diff < 0 : diff > 0;
  return { diff, verdict: !diff ? "same" : better ? "better" : "worse" };
}
