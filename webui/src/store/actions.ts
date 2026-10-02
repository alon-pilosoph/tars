import { del } from "../api";
import { reload } from "./load";
import { confirm, errText, toast } from "./ui";

interface RunOptions {
  revert?: () => void;
  fail?: string;
}

export async function run(fn: () => Promise<unknown>, { revert, fail = "That didn't work" }: RunOptions = {}) {
  try {
    await fn();
    return true;
  } catch (e) {
    revert?.();
    toast(`${fail} (${errText(e)}).`);
    reload(true); // quiet: the toast above already says what went wrong
    return false;
  }
}

/** `done` can be a function, for a message that needs the reloaded data. */
export async function saveThenReload(
  save: () => Promise<unknown>,
  done: string | (() => string),
  options: RunOptions & { ms?: number } = {},
) {
  return run(async () => {
    await save();
    if (await reload()) toast(typeof done === "string" ? done : done(), undefined, options.ms);
  }, options);
}

export async function undoable<T>(
  show: (v: T) => void,
  save: (v: T) => Promise<unknown>,
  next: T,
  prev: T,
  msg: string,
) {
  show(next);
  toast(msg, () => {
    show(prev);
    run(() => save(prev), { revert: () => show(next) });
  });
  await run(() => save(next), { revert: () => show(prev) });
}

export async function confirmDelete(title: string, text: string, path: string, done: string) {
  if (!(await confirm({ title, text, ok: "Delete", danger: true }))) return;
  await saveThenReload(() => del(path), done);
}
