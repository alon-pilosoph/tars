import { post } from "../api";
import { versionName } from "../format";
import { saveThenReload } from "./actions";
import { confirm } from "./ui";

export async function rollback(version: string) {
  const name = versionName(version);
  const ok = await confirm({
    title: `Go back to ${name}?`,
    text: "TARS switches to this wake model and double-check within a few seconds. You can switch back any time.",
    ok: "Use this version",
  });
  if (!ok) return;
  await saveThenReload(() => post("/api/models/use", { version }), `Now using ${name}.`, { fail: "Couldn't switch" });
}
