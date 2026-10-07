import { post } from "../api";
import type { NewReminder, Reminder } from "../types";
import { saveThenReload } from "./actions";
import { reload } from "./load";
import { confirm, errText, toast } from "./ui";

/** Null once it's set; otherwise the server's reason, for the form to show. */
export async function setReminder(r: NewReminder): Promise<string | null> {
  try {
    await post("/api/reminders", r);
  } catch (e) {
    return errText(e);
  }
  if (await reload()) toast("Set. TARS will say it when it's due.");
  return null;
}

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
    text: `TARS won't say “${r.says}”.`,
    ok: "Stop it",
    no: "Keep it",
    danger: true,
  });
  if (ok)
    await saveThenReload(() => post(`/api/reminders/${r.id}/cancel`), "Stopped. TARS won't say it.", {
      fail: "Couldn't stop it",
    });
}
