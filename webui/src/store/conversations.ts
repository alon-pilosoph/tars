import { post } from "../api";
import { stamp } from "../format";
import type { Conversation, Label, Turn } from "../types";
import { confirmDelete, undoable } from "./actions";
import { get, set, setNow } from "./core";
import { eventById, findTurn, speakerName } from "./selectors";
import { copyText, scrollToEl } from "./ui";

const patchTurn = (id: number, patch: Partial<Turn>) =>
  set(s => ({
    convs: s.convs.map(c =>
      c.turns.some(t => t.id === id) ? { ...c, turns: c.turns.map(t => (t.id === id ? { ...t, ...patch } : t)) } : c,
    ),
  }));

const patchWakeLabel = (id: number, label: Label | null) =>
  set(s => ({
    events: s.events.map(e => (e.id === id ? { ...e, label } : e)),
    convs: s.convs.map(c => (c.wake?.event_id === id ? { ...c, wake: { ...c.wake, label } } : c)),
  }));

export const expand = (id: number) => set(s => ({ open: new Set([...s.open, id]) }));

export function goConv(id: number | null) {
  if (id == null) return;
  setNow(s => ({ tab: "home", person: "all", open: new Set([...s.open, id]) }));
  scrollToEl(`.conv[data-id="${id}"]`);
}

export async function delConv(id: number) {
  const s = get();
  const c = s.convs.find(x => x.id === id);
  const who = c && speakerName(s, c.speaker);
  const which = c ? `**${stamp(c.started)}${who ? `, ${who}` : ""}**. ` : "";
  await confirmDelete(
    "Delete this conversation?",
    `${which}Its audio and transcript are deleted for everyone at home. This can't be undone.`,
    `/api/conversations/${id}`,
    "Conversation deleted.",
  );
}

export async function rate(id: number, value: "good" | "bad") {
  const [t] = findTurn(get(), id);
  if (!t) return;
  const next = t.rating === value ? null : value;
  const msg = next === "good" ? "Rated good." : next === "bad" ? "Rated bad." : "Rating cleared.";
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
  const [t] = findTurn(get(), id);
  const text = value.trim();
  if (!t) return;
  set({ edit: null });
  if (!text || text === (t.corrected_text || t.text)) return;
  await undoable(
    c => patchTurn(id, { corrected_text: c }),
    c => post(`/api/turns/${id}/correction`, { text: c ?? t.text }),
    text === t.text ? null : text,
    t.corrected_text ?? null,
    "Transcript fixed.",
  );
}

export async function copyTranscript(c: Conversation | undefined) {
  if (!c) return;
  const s = get();
  const who = (t: Turn) =>
    t.role === "tars" ? "TARS" : speakerName(s, t.speaker) || speakerName(s, c.speaker) || "Someone";
  const lines = c.turns.map(t => `${who(t)}: ${t.corrected_text || t.text}`);
  await copyText(`${stamp(c.started)}\n${lines.join("\n")}`, "Transcript copied.");
}

function saveLabel(id: number, prev: Label | null, next: Label | null, msg: string) {
  return undoable(
    l => patchWakeLabel(id, l),
    l => post(`/api/events/${id}/label`, { label: l }),
    next,
    prev,
    msg,
  );
}

const ANSWERED = { real: "Marked: it was “hey TARS”.", not_real: "Marked: not TARS." };

export async function answerWake(id: number, value: Label) {
  const e = eventById(get(), id);
  if (!e) return;
  const next = e.label === value ? null : value;
  await saveLabel(id, e.label, next, next ? ANSWERED[next] : "Answer cleared.");
}

/** Going back clears the label rather than setting "real": TARS's own guess already counts a wake that a request
    followed as real. */
export async function toggleWakeNotForTars(c: Conversation) {
  const wake = c.wake;
  if (!wake) return;
  const next = wake.label === "not_real" ? null : "not_real";
  const msg = next ? "Marked: that wake wasn't for TARS." : "Marked: it was for TARS.";
  await saveLabel(wake.event_id, wake.label, next, msg);
}
