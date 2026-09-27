import { post } from "../api";
import { reload } from "./load";
import { confirm, errText, toast } from "./ui";

export async function rollback(version: string) {
  if (
    !(await confirm({
      title: `Go back to ${version}?`,
      text: "TARS switches to this wake model and double-check within a few seconds. You can switch back any time.",
      ok: "Use this version",
    }))
  )
    return;
  try {
    await post("/api/models/use", { version });
    toast(`Now using ${version}.`);
  } catch (e) {
    toast(`Couldn't switch (${errText(e)}).`);
  }
  await reload();
}
