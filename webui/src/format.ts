import type { Item, TarsEvent } from "./types";

export type Kind = "answer" | "ask" | "ignore" | "near_miss";

export const ENROLL_AT = 5;
export const KIND: Record<Kind, string> = {
  answer: "Answered",
  ask: "Asked",
  ignore: "Ignored",
  near_miss: "Near-miss",
};
export const IKIND = { link: "Link", note: "Note", list: "List", file: "File" } as const;

export const kindOf = (e: TarsEvent): Kind => (e.kind === "near_miss" ? "near_miss" : (e.outcome ?? "answer"));
export const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
const date = (ts: number) => new Date(ts * 1000);
export const isToday = (ts: number) => sameDay(date(ts), new Date());
const isYesterday = (ts: number) => sameDay(date(ts), new Date(Date.now() - 864e5));
export const time = (ts: number) => date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
export const day = (ts: number) =>
  isToday(ts)
    ? "Today"
    : isYesterday(ts)
      ? "Yesterday"
      : date(ts).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
/** "Yesterday", or the weekday before that ("Monday"). */
export const shortDay = (ts: number) =>
  isToday(ts) ? "Today" : isYesterday(ts) ? "Yesterday" : date(ts).toLocaleDateString([], { weekday: "long" });
export const stamp = (ts: number) => `${day(ts)}, ${time(ts)}`;
/** For a sentence: "today at 7:50 AM", "yesterday at 9:02 PM", "on Monday, Sep 22 at 8:15 PM". */
export const when = (ts: number) =>
  `${isToday(ts) ? "today" : isYesterday(ts) ? "yesterday" : `on ${day(ts)}`} at ${time(ts)}`;
/** "a request followed" -> "A request followed." (no second stop after a question or a quote that ends one) */
export const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1) + (/[.?!]['"”’]?$/.test(s) ? "" : ".");

export const fmtSize = (b: number | null | undefined) =>
  b == null ? "" : b < 1024 ? `${b} B` : b < 1048576 ? `${Math.round(b / 1024)} KB` : `${(b / 1048576).toFixed(1)} MB`;
export const itemName = (i: Item) => i.title || i.name || "";
