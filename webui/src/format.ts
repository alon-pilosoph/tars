import type {
  AnsweredBy,
  CheckLine,
  ConversationWake,
  FailedAt,
  Item,
  MachineHealth,
  Metric,
  ProcessHealth,
  Reminder,
  StatusPage,
  Turn,
} from "./types";

export const COPY_LABEL = { link: "Copy link", note: "Copy text", list: "Copy list" } as const;

/** The server's version name for the models TARS was installed with. */
export const INSTALLED = "installed";
export const versionName = (v: string) => (v === INSTALLED ? "the installed models" : v);

/** The double-check writes a word it didn't know as "[unk]". */
export const heardText = (s: string) => s.replace(/\[unk\]/g, "…");
export const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
const date = (ts: number) => new Date(ts * 1000);
export const isToday = (ts: number) => sameDay(date(ts), new Date());
const isYesterday = (ts: number) => sameDay(date(ts), new Date(Date.now() - 864e5));
const isTomorrow = (ts: number) => sameDay(date(ts), new Date(Date.now() + 864e5));
export const time = (ts: number) => date(ts).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = "January February March April May June July August September October November December".split(" ");
export function longDate(ts: number) {
  const d = date(ts);
  return `${WEEKDAYS[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function day(ts: number) {
  if (isToday(ts)) return "Today";
  if (isYesterday(ts)) return "Yesterday";
  if (isTomorrow(ts)) return "Tomorrow";
  return longDate(ts);
}
export const isRecent = (ts: number) => isToday(ts) || isYesterday(ts);

export const stamp = (ts: number) => `${day(ts)}, ${time(ts)}`;

export function when(ts: number) {
  const dayPart = isToday(ts) ? "today" : isYesterday(ts) ? "yesterday" : isTomorrow(ts) ? "tomorrow" : `on ${day(ts)}`;
  return `${dayPart} at ${time(ts)}`;
}

/** Capitalized, with a full stop unless it already ends one (also inside a closing quote). */
export const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1) + (/[.?!]['"”’]?$/.test(s) ? "" : ".");

export function fmtSize(bytes: number | null | undefined) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${(bytes / 1073741824).toFixed(1)} GB`;
}

/** How long, in the one unit that reads best: "40 seconds", "12 minutes", "5 hours", "3 days". */
export function duration(seconds: number) {
  if (seconds < 90) return plural(Math.max(1, Math.round(seconds)), "second");
  if (seconds < 90 * 60) return plural(Math.round(seconds / 60), "minute");
  if (seconds < 36 * 3600) return plural(Math.round(seconds / 3600), "hour");
  return plural(Math.round(seconds / 86400), "day");
}

export const ago = (ts: number, now: number) => (now - ts < 10 ? "just now" : `${duration(now - ts)} ago`);

export const itemName = (i: Item) => i.title || i.name || "";

const ANSWERED_BY: Record<AnsweredBy, string> = {
  quick: "Answered by the quick model (Qwen).",
  look_up: "Handed over to OpenAI, which looked it up.",
  ponder: "Handed over to the thinking model, to work it through.",
  fallback: "Answered by OpenAI, as a backup.",
  openai: "Answered by OpenAI.",
};
const FAILED_AT: Record<FailedAt, string> = {
  stt: "Failed while hearing what was said.",
  llm: "Failed while writing the answer.",
  tts: "Failed while saying the answer.",
  other: "Failed before it could answer.",
};
const secs = (s: number) => (s < 1 ? s.toFixed(2) : s.toFixed(1));

export const failedLine = (t: Turn) => (t.failed_at ? (FAILED_AT[t.failed_at] ?? FAILED_AT.other) : "");

export function timingLine(t: Turn) {
  const x = t.timings;
  const out: string[] = [];
  if (x?.total != null) {
    const parts = [
      x.end_of_speech != null ? `${secs(x.end_of_speech)} s of silence` : "",
      x.stt != null ? `${secs(x.stt)} s to write down what was said` : "",
      x.llm != null ? `${secs(x.llm)} s to think of the first sentence` : "",
      x.tts != null ? `${secs(x.tts)} s to start speaking` : "",
    ].filter(Boolean);
    out.push(
      `Took ${secs(x.total)} s from the end of speech to the first sound${parts.length ? `: ${parts.join(", ")}` : ""}.`,
    );
  }
  if (x?.greet != null) out.push(`Greeted ${secs(x.greet)} s after the wake word.`);
  if (t.answered_by) out.push(ANSWERED_BY[t.answered_by] ?? "");
  if (t.error) out.push(`The error was “${t.error}”.`);
  return out.filter(Boolean).join(" ");
}

export function wakeLine(w: ConversationWake | null, voice: { name: string | null; known: boolean }) {
  if (!w) return "";
  const heard = w.heard ? `“${heardText(w.heard)}”` : "its name";
  const sure = w.confidence != null ? `${Math.round(w.confidence * 100)}%` : null;
  const line =
    w.outcome === "ask"
      ? `TARS heard ${heard}, was only ${sure ?? "a little"} sure, so it asked “Did you call me?” first.`
      : `TARS heard ${heard}${sure ? ` and was ${sure} sure` : ""}.`;
  if (voice.name) return `${line} It recognised ${voice.name} by voice.`;
  return voice.known ? line : `${line} It didn't recognise the voice.`;
}

export type Verdict = "better" | "same" | "worse";

export function metricChange(m: Metric): { diff: number; verdict: Verdict } | null {
  const before = parseFloat(m.current);
  const after = parseFloat(m.candidate);
  if (isNaN(before) || isNaN(after)) return null;
  const diff = after - before;
  const better = m.lower_is_better ? diff < 0 : diff > 0;
  return { diff, verdict: !diff ? "same" : better ? "better" : "worse" };
}

export const REMINDER_KIND_LABEL = { timer: "Timer", reminder: "Reminder", message: "Message" } as const;

/** "stacey" → "Stacey", "mary ann" → "Mary Ann", as TARS says names. */
export const personName = (n: string) =>
  n
    .split(/\s+/)
    .filter(Boolean)
    .map(w => w[0].toUpperCase() + w.slice(1))
    .join(" ");

export function reminderNow(r: Reminder) {
  if (r.kind === "timer") return `Ringing since ${time(r.due ?? r.last_said ?? r.created)}`;
  const again = r.next_at != null ? `, again at ${time(r.next_at)}` : "";
  return `Said ${r.tries} of ${r.max_tries} times${again}. Waiting for “got it”.`;
}

export function reminderWhen(r: Reminder) {
  if (r.next_at == null) return `When ${personName(r.for_name || "they")} is next heard`;
  if (r.due != null && r.next_at > r.due + 30) return `Snoozed until ${time(r.next_at)}`;
  return stamp(r.next_at);
}

export function reminderEnded(r: Reminder) {
  switch (r.status) {
    case "acknowledged": {
      const at = r.acked_at != null ? ` ${when(r.acked_at)}` : "";
      if (r.acked_via === "web") return `Got it on this page${at}.`;
      if (r.acked_via === "telegram") return `${personName(r.acked_by ?? "Someone")} said “got it” on Telegram${at}.`;
      return `${r.acked_by ? personName(r.acked_by) : "An unknown voice"} said “got it”${at}.`;
    }
    case "said":
      return `${r.channel === "telegram" ? "Sent on Telegram" : "Said"} ${r.last_said != null ? when(r.last_said) : "earlier"}.`;
    case "missed":
      return r.kind === "timer"
        ? `Missed. It rang for ${plural(Math.round((r.tries * r.repeat_every_s) / 60), "minute")} and nobody turned it off.`
        : `Missed. Said ${plural(r.tries, "time")} and nobody said “got it”.`;
    case "cancelled":
      return r.due != null ? `Stopped. It was due ${when(r.due)}.` : "Stopped.";
    default:
      return "";
  }
}

/** How it was set: "set by voice", "set on Telegram", "set on this page". */
export function reminderSetVia(r: Reminder) {
  return { voice: "set by voice", telegram: "set on Telegram", web: "set on this page" }[r.set_via];
}

/** Where one still to come is said, when that isn't aloud at home (once said, reminderEnded says where). */
export function reminderWhere(r: Reminder) {
  return r.channel === "telegram" && r.status === "scheduled" ? " It's sent on Telegram, not said aloud." : "";
}

export function reminderFor(r: Reminder) {
  // One for no one in particular, posted on Telegram, goes to whoever set it.
  const forName = r.for_name || (r.channel === "telegram" ? r.from_name : null);
  const parts = [`${REMINDER_KIND_LABEL[r.kind]} for ${forName ? personName(forName) : "whoever's there"}`];
  if (r.from_name && r.from_name.toLowerCase() !== forName?.toLowerCase())
    parts.push(`from ${personName(r.from_name)}`);
  return parts.join(", ");
}

/* The Status page. A line is `bad` when something needs looking at, which is all the accent colour is for. */

export interface HealthLine {
  text: string;
  bad: boolean;
}

export function processLine(p: ProcessHealth, now: number): HealthLine {
  const unit = p.unit_state && p.unit_state !== "active" ? `; systemd says ${p.unit_state}` : "";
  if (p.running && p.started != null) {
    const commit = p.version ? `, commit ${p.version}` : "";
    return { text: `Running for ${duration(now - p.started)}${commit}${unit}`, bad: Boolean(unit) };
  }
  if (p.beat != null) return { text: `Stopped: last heard from ${ago(p.beat, now)}${unit}`, bad: true };
  return { text: `Not seen running${unit}`, bad: true };
}

export function pageLine(page: StatusPage | null): HealthLine {
  if (!page) return { text: "No status page", bad: false };
  return { text: page.description, bad: page.indicator !== "none" && page.indicator !== "unknown" };
}

const MARK = { ok: "✓", warn: "!", fail: "✗" } as const; // check.py's

/** A check's verdict as --check prints it, and what to do about it only when it isn't fine. */
export function checkLine(c: CheckLine) {
  const bad = c.status !== "ok";
  return { mark: MARK[c.status], text: c.detail, fix: bad ? c.fix : "", bad };
}

export function processorLine(m: MachineHealth) {
  return `${Math.round(m.cpu_percent)}% busy; load ${m.load.join(", ")} on ${plural(m.cores, "core")}`;
}

export const temperatureLine = (m: MachineHealth) =>
  m.temperature_c != null ? `${m.temperature_c.toFixed(1)} °C` : "Not reported here";

export function memoryLine(m: MachineHealth) {
  return `${fmtSize(m.memory_used)} of ${fmtSize(m.memory_total)} (${Math.round((m.memory_used / m.memory_total) * 100)}%)`;
}

export function diskLine(m: MachineHealth) {
  const used = Math.round((1 - m.disk_free / m.disk_total) * 100);
  return `${fmtSize(m.disk_free)} free of ${fmtSize(m.disk_total)} (${used}% used)`;
}
