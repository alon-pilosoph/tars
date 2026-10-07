import { post } from "../api";
import { plural } from "../format";
import { confirmDelete, saveThenReload } from "./actions";
import { type State, get, set } from "./core";
import { clusterById, clusterName, eventById, voiceName } from "./selectors";
import { prompt } from "./ui";

const NAME_MAX_LENGTH = 60; // the server's limit
const RECLUSTER_TIMEOUT_MS = 300_000; // regrouping every request can take minutes on a Pi

export async function moveTo(eventId: number, clusterId: number) {
  await saveThenReload(
    () => post(`/api/events/${eventId}/cluster`, { cluster_id: clusterId }),
    () => `Moved to ${clusterName(get(), clusterId)}. It stays there when voices are regrouped.`,
  );
}

export async function newVoice(eventId: number) {
  const said = eventById(get(), eventId)?.transcript;
  const name = await prompt({
    title: "Whose voice is it?",
    text: `A new voice${said ? ` for “${said}”` : ""}. TARS learns it from this and later requests.`,
    placeholder: "Stacey",
    ok: "Save",
    required: true,
    maxLength: NAME_MAX_LENGTH,
  });
  if (!name) return;
  let created: number | null = null;
  await saveThenReload(
    async () => {
      created = (await post<{ id: number }>("/api/clusters", { name })).id;
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
  await confirmDelete(
    "Delete this wake?",
    "The recording goes, and TARS won't learn from it. This can't be undone.",
    `/api/events/${id}`,
    "Deleted the wake.",
  );
}

export async function rename(id: number) {
  const s = get();
  const c = clusterById(s, id);
  if (!c) return;
  const enrollAt = s.status.enroll_at;
  const name = await prompt({
    title: c.name ? `Rename ${c.name}` : `Name ${voiceName(c)}`,
    text: "TARS will greet them by name, and their requests will say who asked.",
    placeholder: "Stacey",
    value: c.name || "",
    samples: c.samples.slice(0, 2),
    ok: "Save",
    required: true,
    maxLength: NAME_MAX_LENGTH,
  });
  if (!name) return;
  let saved;
  if (c.kind === "not_person") saved = "Saved.";
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

export const openMerge = (id: number) => set({ dialog: { kind: "merge", id } });

export function mergeKeeps(s: State, a: number, b: number) {
  return !!clusterById(s, b)?.name && !clusterById(s, a)?.name ? b : a;
}

export async function merge(keep: number, absorb: number) {
  const keepName = clusterName(get(), keep);
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
