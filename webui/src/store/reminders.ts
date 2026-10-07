import { post } from "../api";
import type { NewReminder, Reminder } from "../types";
import { saveThenReload } from "./actions";
import { confirm } from "./ui";

/** True if it was set; the page keeps the form otherwise, with the server's reason in a toast. */
export const setReminder = (r: NewReminder) =>
  saveThenReload(() => post("/api/reminders", r), "Set. TARS will say it when it's due.", { fail: "Couldn't set it" });

export const ackReminder = (r: Reminder) =>
  saveThenReload(() => post(`/api/reminders/${r.id}/ack`), "Marked as got it.", { fail: "Couldn't mark it" });

export const snoozeReminder = (r: Reminder, minutes: number) =>
  saveThenReload(
    () => post(`/api/reminders/${r.id}/snooze`, { minutes }),
    `TARS will say it again in ${minutes} minutes.`,
    { fail: "Couldn't snooze it" },
  );

/** "Stop", not "Cancel": the dialog's own Cancel button means "never mind". */
export async function cancelReminder(r: Reminder) {
  const ok = await confirm({
    title: "Stop this reminder?",
    text: `TARS won't say “${r.says}”`,
    ok: "Stop it",
    danger: true,
  });
  if (ok) await saveThenReload(() => post(`/api/reminders/${r.id}/cancel`), "Stopped.", { fail: "Couldn't stop it" });
}
