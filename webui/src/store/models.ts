import { post } from "../api";
import { versionName } from "../format";
import { saveThenReload } from "./actions";
import { confirm } from "./ui";

export async function rollback(version: string) {
  const name = versionName(version);
  const ok = await confirm({
    title: `Switch back to ${name}?`,
    text: `TARS will listen with ${name} from now on. You can switch again here at any time.`,
    ok: `Use ${name}`,
  });
  if (!ok) return;
  await saveThenReload(() => post("/api/models/use", { version }), `Now using ${name}.`, { fail: "Couldn't switch" });
}
