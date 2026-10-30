import { post } from "../api";
import { answerSplit, stamp } from "../format";
import type { Conversation, Label, TarsEvent, Turn } from "../types";
import { confirmDelete, saveThenReload, undoable } from "./actions";
import { get, set, setNow } from "./core";
import { eventById, findTurn, speakerName } from "./selectors";
import { confirm, copyText, scrollToEl } from "./ui";

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

export function toggleOpen(id: number) {
  set(s => {
    const open = new Set(s.open);
    if (!open.delete(id)) open.add(id);
    const editing = s.edit != null && s.convs.find(c => c.id === id)?.turns.some(t => t.id === s.edit);
    return { open, edit: editing && !open.has(id) ? null : s.edit };
  });
}

export const showMoreTurns = (id: number) => set(s => ({ moreTurns: new Set([...s.moreTurns, id]) }));

export function goConv(id: number | null) {
  if (id == null) return;
  setNow(s => ({ tab: "home", person: "all", open: new Set([...s.open, id]) }));
  scrollToEl(`.conv[data-id="${id}"]`);
}

export async function delConv(id: number) {
  await confirmDelete(
    "Delete this conversation?",
    "Its recordings go too, and so does what TARS sent in it. This can't be undone.",
    `/api/conversations/${id}`,
    "Deleted the conversation.",
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

let focusFix = false;
export function startFix(id: number) {
  focusFix = true;
  set({ edit: id });
}
export function takeFixFocus() {
  const was = focusFix;
  focusFix = false;
  return was;
}
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

/** Review's "agree with all": TARS's guess becomes the answer for each of these nobody has answered, after saying
    how they split. The server works the guesses out again, leaves alone any answered meanwhile, and says what it
    saved. */
export async function agreeWithGuesses(events: TarsEvent[]) {
  const guessed = events.filter(e => !e.label && e.auto_label);
  const n = guessed.length;
  const split = answerSplit({
    real: guessed.filter(e => e.auto_label === "real").length,
    not_real: guessed.filter(e => e.auto_label === "not_real").length,
  });
  const ok = await confirm({
    title: `Agree with all ${n} guesses?`,
    text:
      `**${split}.** Each gets that answer, as if you'd tapped it: they become what the next wake model learns ` +
      "from. Answers you've given are kept. Listen to any you're unsure of first.",
    ok: `Agree with ${n}`,
  });
  if (!ok) return;
  let saved: Record<Label, number> = { real: 0, not_real: 0 };
  await saveThenReload(
    async () => {
      saved = await post<Record<Label, number>>("/api/events/agree", { ids: guessed.map(e => e.id) });
    },
    () => {
      const total = saved.real + saved.not_real;
      return total ? `Answered ${total}: ${answerSplit(saved)}.` : "Nothing to answer: they'd all been answered.";
    },
    { fail: "Couldn't answer them" },
  );
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
