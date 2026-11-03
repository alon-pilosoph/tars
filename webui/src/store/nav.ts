import { stopAudio } from "../audio";
import type { GuessFilter, PersonFilter, Tab } from "../params";
import { set, setNow } from "./core";

export function setTab(tab: Tab) {
  stopAudio();
  setNow({ tab });
  scrollTo(0, 0);
}

export function setPerson(person: PersonFilter) {
  set({ person });
}

export function setGuess(guess: GuessFilter) {
  set({ guess });
}
