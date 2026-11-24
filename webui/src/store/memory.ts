import { del, post } from "../api";
import type { Memory, NeverLearn } from "../types";
import { saveThenReload } from "./actions";
import { confirm, prompt } from "./ui";

export async function editMemory(m: Memory) {
  const text = await prompt({
    title: "Put it right",
    label: "What TARS knows",
    required: true,
    value: m.text,
    placeholder: "What TARS should know",
    maxLength: 300,
    ok: "Save",
  });
  if (text && text !== m.text)
    await saveThenReload(() => post(`/api/memories/${m.id}`, { text }), "Saved.", { fail: "Couldn't save it" });
}

export async function forgetMemory(m: Memory) {
  const ok = await confirm({
    title: "Forget this?",
    text: "It's deleted, with what it replaced. If it comes up again, TARS may learn it again.",
    ok: "Forget",
    no: "Keep it",
    danger: true,
  });
  if (ok) await saveThenReload(() => del(`/api/memories/${m.id}`), "Forgotten.", { fail: "Couldn't forget it" });
}

export async function neverLearn(m: Memory) {
  const ok = await confirm({
    title: "Never learn this?",
    text: "TARS forgets it and won't pick it up again by itself. Asking TARS to remember it still works.",
    ok: "Never learn it",
    no: "Keep it",
    danger: true,
  });
  if (ok)
    await saveThenReload(() => post(`/api/memories/${m.id}/never`, {}), "Forgotten, and not to be learned again.", {
      fail: "Couldn't forget it",
    });
}

export const allowAgain = (n: NeverLearn) =>
  saveThenReload(() => del(`/api/never/${n.id}`), "TARS may learn it again.", { fail: "Couldn't change it" });
