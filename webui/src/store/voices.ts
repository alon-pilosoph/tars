import { post } from "../api";
import { heardText, plural, stamp } from "../format";
import { confirmDelete, saveThenReload } from "./actions";
import { get, set } from "./core";
import { clusterById, clusterName, eventById, voiceName } from "./selectors";
import { confirm, prompt } from "./ui";

const NAME_MAX_LENGTH = 60; // the server's limit
const RECLUSTER_TIMEOUT_MS = 300_000; // regrouping every request can take minutes on a Pi

export async function moveTo(eventId: number, clusterId: number) {
  await saveThenReload(
    () => post(`/api/events/${eventId}/cluster`, { cluster_id: clusterId }),
    () => `Moved to ${clusterName(get(), clusterId)}. It stays there when voices are regrouped.`,
  );
}

export async function newVoice(eventId: number) {
  const name = await prompt({
    title: "New voice",
    text: "Move this request to a new voice. You can name it now or later.",
    placeholder: "Name (optional)",
    ok: "Create",
    maxLength: NAME_MAX_LENGTH,
  });
  if (name === null) return;
  let created: number | null = null;
  await saveThenReload(
    async () => {
      created = (await post<{ id: number }>("/api/clusters", { name: name || null })).id;
      await post(`/api/events/${eventId}/cluster`, { cluster_id: created });
    },
    () => `Moved to ${clusterName(get(), created)}.`,
    {
      revert: () => {
        if (created != null) removeEmptyVoice(created, eventId);
      },
    },
  );
}

/** Takes back a new voice the request couldn't be moved to. There's no delete for a voice, but merging it, empty,
    into another removes it and moves nothing. */
function removeEmptyVoice(id: number, eventId: number) {
  const s = get();
  const into = eventById(s, eventId)?.cluster_id ?? s.clusters.find(c => c.id !== id)?.id;
  if (into != null) post("/api/clusters/merge", { keep: into, absorb: id }).catch(() => {});
}

export async function delEvent(id: number) {
  const e = eventById(get(), id);
  const heard = e?.heard ? `, heard “${heardText(e.heard)}”` : "";
  const which = e ? `**${stamp(e.ts)}**${heard}. ` : "";
  await confirmDelete(
    "Delete this wake?",
    `${which}Its audio is deleted too. This can't be undone.`,
    `/api/events/${id}`,
    "Wake deleted.",
  );
}

export async function rename(id: number) {
  const s = get();
  const c = clusterById(s, id);
  if (!c) return;
  const enrollAt = s.status.enroll_at;
  const said = !c.name && c.samples.find(x => x.transcript)?.transcript;
  const asked = said ? `, asked “${said}”` : "";
  const about = c.name ? "" : `**${voiceName(c)}**, ${plural(c.size, "request")}${asked}. `;
  const name = await prompt({
    title: c.name ? `Rename ${c.name}` : "Whose voice is this?",
    text:
      `${about}Once a named voice has ${enrollAt} or more requests, TARS builds a voiceprint and greets them ` +
      "by name.",
    placeholder: "Name",
    value: c.name || "",
    ok: "Save",
    required: !c.name,
    maxLength: NAME_MAX_LENGTH,
  });
  if (name === null) return;
  let saved;
  if (!name) saved = "Name removed.";
  else if (c.kind === "not_person") saved = "Saved.";
  else if (c.size >= enrollAt) saved = `Saved. Regroup voices to build ${name}'s voiceprint.`;
  else saved = `Saved. ${plural(enrollAt - c.size, "more request")} for ${name}'s voiceprint.`;
  // No kind: the server keeps "not a person" (a named TV stays a TV) and otherwise follows the name.
  await saveThenReload(() => post(`/api/clusters/${id}`, { name: name || null }), saved);
}

export async function toggleNotPerson(id: number) {
  const c = clusterById(get(), id);
  if (!c) return;
  const notPerson = c.kind !== "not_person";
  const kind = notPerson ? "not_person" : c.name ? "person" : "unknown";
  await saveThenReload(
    () => post(`/api/clusters/${id}`, { name: c.name, kind }),
    notPerson ? `${voiceName(c)} marked as not a person.` : "Marked as a person.",
  );
}

/** Merging a named voice into an unnamed one goes the other way, so the name isn't lost with the voice merged
    away. */
export async function merge(from: number, into: number) {
  const s = get();
  const swap = !!clusterById(s, from)?.name && !clusterById(s, into)?.name;
  const absorb = swap ? into : from;
  const keep = swap ? from : into;
  const absorbName = clusterName(s, absorb);
  const keepName = clusterName(s, keep);
  const ok = await confirm({
    title: `Merge ${absorbName} into ${keepName}?`,
    text: `Everything in **${absorbName}** moves to **${keepName}**, and stays there when voices are regrouped.`,
    ok: "Merge",
  });
  if (!ok) return;
  await saveThenReload(() => post("/api/clusters/merge", { keep, absorb }), `Merged into ${keepName}.`);
}

export async function recluster() {
  set({ reclustering: true });
  let summary = "";
  try {
    await saveThenReload(
      async () => {
        summary = (await post<{ summary?: string }>("/api/recluster", undefined, RECLUSTER_TIMEOUT_MS)).summary || "";
      },
      () => summary || "Done.",
      { fail: "Regrouping failed", ms: 8000 },
    );
  } finally {
    set({ reclustering: false });
  }
}
