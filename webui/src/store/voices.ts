import { post } from "../api";
import { ENROLL_AT, plural } from "../format";
import { confirmDelete, run } from "./actions";
import { get, set } from "./core";
import { reload } from "./load";
import { cName, voiceName } from "./selectors";
import { confirm, errText, prompt, toast } from "./ui";

export const moveTo = (id: number, cid: number) =>
  run(async () => {
    await post(`/api/events/${id}/cluster`, { cluster_id: cid });
    await reload();
    toast(`Moved to ${cName(get(), cid)}. Pinned: re-clustering leaves it there.`);
  });

export async function newVoice(id: number) {
  const name = await prompt({
    title: "New voice",
    text: "Move this request to a new voice. You can name it now or later.",
    placeholder: "Name (optional)",
    ok: "Create",
  });
  if (name === null) return;
  await run(async () => {
    const cid = (await post<{ id: number }>("/api/clusters", { name: name || null })).id;
    await post(`/api/events/${id}/cluster`, { cluster_id: cid });
    await reload();
    toast(`Moved to ${cName(get(), cid)}.`);
  });
}

export async function delEvent(id: number) {
  await confirmDelete(
    "Delete this event?",
    "Its audio is deleted too. This can't be undone.",
    `/api/events/${id}`,
    "Event deleted.",
  );
}

export async function rename(id: number) {
  const c = get().clusters.find(x => x.id === id) ?? { id, name: null, kind: "unknown" as const, size: 0 };
  const name = await prompt({
    title: c.name ? `Rename ${c.name}` : "Whose voice is this?",
    text: `Once a named voice has ${ENROLL_AT} or more requests, TARS builds a voiceprint and greets them by name.`,
    placeholder: "Name",
    value: c.name || "",
    ok: "Save",
  });
  if (name === null) return;
  const saved = !name
    ? "Name removed."
    : c.kind === "not_person"
      ? "Saved."
      : c.size >= ENROLL_AT
        ? `Saved. Re-cluster to build ${name}'s voiceprint.`
        : `Saved. ${plural(ENROLL_AT - c.size, "more request")} for ${name}'s voiceprint.`;
  await run(async () => {
    // No kind: the server keeps "not a person" (a named TV stays a TV) and otherwise follows the name.
    await post(`/api/clusters/${id}`, { name: name || null });
    await reload();
    toast(saved);
  });
}

export async function toggleNotPerson(id: number) {
  const c = get().clusters.find(x => x.id === id);
  if (!c) return;
  const notPerson = c.kind !== "not_person";
  await run(async () => {
    await post(`/api/clusters/${id}`, { name: c.name, kind: notPerson ? "not_person" : c.name ? "person" : "unknown" });
    await reload();
    toast(notPerson ? `${voiceName(c)} marked as not a person.` : "Marked as a person.");
  });
}

export async function merge(absorb: number, keep: number) {
  const s = get(),
    a = cName(s, absorb),
    b = cName(s, keep);
  if (
    !(await confirm({
      title: `Merge ${a} into ${b}?`,
      text: `Everything in **${a}** moves to **${b}**, and stays there when you re-cluster.`,
      ok: "Merge",
    }))
  )
    return;
  await run(async () => {
    await post("/api/clusters/merge", { keep, absorb });
    await reload();
    toast(`Merged into ${b}.`);
  });
}

export async function recluster() {
  set({ reclustering: true });
  try {
    const r = await post<{ summary?: string }>("/api/recluster");
    set({ reclustering: false });
    await reload();
    toast(r.summary || "Done.", undefined, 8000);
  } catch (e) {
    set({ reclustering: false });
    toast(`Re-clustering failed (${errText(e)}).`);
  }
}
