import { del } from "../api";
import { reload } from "./load";
import { confirm, errText, toast } from "./ui";

/** Runs an action; if it fails, says so and reloads, which puts back anything the page changed ahead of the server. */
export async function run<T>(fn: () => Promise<T>, fail = "That didn't work. Try again?") {
  try {
    return await fn();
  } catch (e) {
    toast(`${fail} (${errText(e)})`);
    reload();
  }
}

/** Shows a change right away, saves it, and offers Undo, which shows and saves what was there before. */
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
    run(() => save(prev));
  });
  await run(() => save(next));
}

export async function confirmDelete(title: string, text: string, path: string, done: string) {
  if (!(await confirm({ title, text, ok: "Delete", danger: true }))) return;
  await run(async () => {
    await del(path);
    await reload();
    toast(done);
  });
}
