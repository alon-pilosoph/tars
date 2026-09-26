import { post } from "../api";
import { stamp } from "../format";
import type { Conversation, Label, TarsEvent, Turn } from "../types";
import { confirmDelete, undoable } from "./actions";
import { get, set, setNow } from "./core";
import { findTurn, speakerName } from "./selectors";
import { copyText, scrollToEl } from "./ui";

const patchTurn = (id: number, p: Partial<Turn>) =>
  set(s => ({
    convs: s.convs.map(c =>
      c.turns.some(t => t.id === id) ? { ...c, turns: c.turns.map(t => (t.id === id ? { ...t, ...p } : t)) } : c,
    ),
  }));
const patchEvent = (id: number, p: Partial<TarsEvent>) =>
  set(s => ({ events: s.events.map(e => (e.id === id ? { ...e, ...p } : e)) }));

export const expand = (id: number) => set(s => ({ open: new Set([...s.open, id]) }));

export function goConv(id: number | null) {
  if (id == null) return;
  setNow(s => ({ tab: "home", person: "all", open: new Set([...s.open, id]) }));
  scrollToEl(`.conv[data-id="${id}"]`);
}

export async function delConv(id: number) {
  await confirmDelete(
    "Delete this conversation?",
    "Its audio and transcript are deleted for everyone at home. This can't be undone.",
    `/api/conversations/${id}`,
    "Conversation deleted.",
  );
}

export async function rate(id: number, v: "good" | "bad") {
  const [t] = findTurn(get(), id);
  if (!t) return;
  const next = t.rating === v ? null : v;
  const msg =
    next === "good"
      ? "Noted: a good answer."
      : next === "bad"
        ? "Noted: a bad answer. TARS learns from these."
        : "Rating cleared.";
  await undoable(
    r => patchTurn(id, { rating: r }),
    r => post(`/api/turns/${id}/rating`, { rating: r }),
    next,
    t.rating ?? null,
    msg,
  );
}

export const startFix = (id: number) => set({ edit: id });
export const cancelFix = () => set({ edit: null });

export async function saveFix(id: number, value: string) {
  const [t] = findTurn(get(), id),
    v = value.trim();
  if (!t) return;
  set({ edit: null });
  if (!v || v === (t.corrected_text || t.text)) return;
  // Saving exactly what TARS heard clears the correction.
  await undoable(
    c => patchTurn(id, { corrected_text: c }),
    c => post(`/api/turns/${id}/correction`, { text: c ?? t.text }),
    v === t.text ? null : v,
    t.corrected_text ?? null,
    "Fixed. TARS learns from the correction.",
  );
}

export async function copyTranscript(c: Conversation | undefined) {
  if (!c) return;
  const s = get(),
    who = (t: Turn) =>
      t.role === "tars" ? "TARS" : speakerName(s, t.speaker) || speakerName(s, c.speaker) || "Someone";
  const lines = c.turns.map(t => `${who(t)}: ${t.corrected_text || t.text}`);
  await copyText(`${stamp(c.started)}\n${lines.join("\n")}`, "Transcript copied.");
}

/** Was the wake really for TARS? Review, and a conversation's menu (its wake never reaches Review). */
export async function setLabel(
  id: number,
  v: Label,
  messages = { real: "Marked: it was “hey TARS”.", not_real: "Marked: not TARS." },
) {
  const e = get().events.find(x => x.id === id);
  if (!e) return;
  const next = e.label === v ? null : v;
  await undoable(
    l => patchEvent(id, { label: l }),
    l => post(`/api/events/${id}/label`, { label: l }),
    next,
    e.label ?? null,
    next ? messages[next] : "Label cleared.",
  );
}

export async function wakeNotForTars(c: Conversation) {
  if (c.wake?.event_id != null)
    await setLabel(c.wake.event_id, "not_real", { real: "", not_real: "Marked: that wake wasn't for TARS." });
}
