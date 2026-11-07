import { del, post } from "../api";
import type { Preference, Skill } from "../types";
import { saveThenReload } from "./actions";
import { confirm } from "./ui";

export const pauseSkill = (sk: Skill, paused: boolean) =>
  saveThenReload(
    () => post(`/api/skills/${sk.id}/pause`, { paused }),
    paused ? `Paused. ${cap(sk.name)} won't run by itself until it's resumed.` : `${cap(sk.name)} is back on.`,
    { fail: paused ? "Couldn't pause it" : "Couldn't resume it" },
  );

export async function deleteSkill(sk: Skill) {
  const ok = await confirm({
    title: `Delete ${cap(sk.name)}?`,
    text: "TARS will forget it. To have it back, teach it again.",
    ok: "Delete",
    no: "Keep it",
    danger: true,
  });
  if (ok) await saveThenReload(() => del(`/api/skills/${sk.id}`), "Deleted.", { fail: "Couldn't delete it" });
}

export const forgetPreference = (p: Preference) =>
  saveThenReload(() => del(`/api/preferences/${p.id}`), "Forgotten.", { fail: "Couldn't forget it" });

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
