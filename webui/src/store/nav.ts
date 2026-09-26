import { stopAudio } from "../audio";
import type { PersonFilter, Tab } from "../params";
import { set, setNow } from "./core";

export function setTab(tab: Tab) {
  stopAudio();
  setNow({ tab });
  scrollTo(0, 0);
}

export function setPerson(person: PersonFilter) {
  set({ person });
}
