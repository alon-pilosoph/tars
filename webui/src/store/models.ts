import { post } from "../api";
import type { RetrainResult } from "../types";
import { set } from "./core";
import { reload } from "./load";
import { confirm, errText, toast } from "./ui";

export async function retrain() {
  set({ retraining: true });
  try {
    const r = await post<RetrainResult>("/api/retrain");
    set({ lastRetrain: { ...r, ts: r.ts || Date.now() / 1000 } });
  } catch (e) {
    toast(`Retraining failed (${errText(e)}).`);
  }
  set({ retraining: false });
  await reload();
}

export async function rollback(version: string) {
  if (
    !(await confirm({
      title: `Go back to ${version}?`,
      text: "The double-check switches to this version right away. You can switch back any time.",
      ok: "Use this version",
    }))
  )
    return;
  toast("Rollback isn't built yet.");
}
