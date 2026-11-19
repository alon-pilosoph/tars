/* The Voices page's people: loaded when the page is shown and with Refresh while it is, and again after each
   answer, which is final on the server (people.py). */
import { getJson, post } from "../api";
import type { PeoplePage } from "../types";
import { get, set } from "./core";
import { errText, prompt, toast } from "./ui";

const NAME_MAX_LENGTH = 60; // the server's limit

let loading: Promise<void> | null = null;

export function loadPeople() {
  loading ??= (async () => {
    try {
      set({ people: await getJson<PeoplePage>("/api/people"), peopleError: "" });
    } catch (e) {
      set({ peopleError: errText(e) || "Failed to fetch" });
    } finally {
      loading = null;
    }
  })();
  return loading;
}

/** Once the Voices page's load, if one is under way, has landed. */
export const peopleLoaded = () => loading ?? Promise.resolve();

/** Whose request it is: a person's id, "tv", or "unsure". The card goes at once; the page catches up after. */
export async function answerWho(eventId: number, to: number | "tv" | "unsure", said: string) {
  const page = get().people;
  if (page) set({ people: { ...page, ask: page.ask.filter(c => c.event_id !== eventId) } });
  try {
    await post("/api/people/answer", { event_id: eventId, to });
    toast(said);
  } catch (e) {
    toast(`Couldn't save that: ${errText(e)}`);
  }
  await loadPeople();
}

/** Someone new: by name, and this request is theirs, if there is one. */
export async function someoneNew(eventId?: number) {
  const name = await prompt({
    title: eventId ? "Who said it?" : "Add someone",
    text: "Their name, as TARS should greet them.",
    placeholder: "Stacey",
    ok: "Save",
    required: true,
    maxLength: NAME_MAX_LENGTH,
  });
  if (!name) return;
  try {
    const { id } = await post<{ id: number }>("/api/people", { name });
    if (eventId) await answerWho(eventId, id, `Saved as ${name}.`);
    else {
      toast(`Added ${name}.`);
      await loadPeople();
    }
  } catch (e) {
    toast(`Couldn't add ${name}: ${errText(e)}`);
  }
}
