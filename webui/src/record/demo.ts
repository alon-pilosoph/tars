/* The recording page's sample session, with no server: ?demo&session=house|voice. The take's meter moves, it stops
   by itself after a few seconds as TARS would, and every fourth take comes back too quiet, to show a redo.

   For the screenshot tests: &done=<n> prompts already recorded, and &step=ready|recording|heard|problem|busy|done|gone
   to open the page at that step. */
import { PARAMS } from "../params";
import type { Step } from "./Record";
import type { Prompt, Recording, Session, Take } from "./types";

const HOUSE: Prompt[] = [
  { id: "h1", say: "Turn on the AC in the bedroom", how: "" },
  { id: "h2", say: "Lock the front door", how: "From across the room" },
  { id: "h3", say: "Set the living room AC to twenty three", how: "" },
  { id: "h4", say: "Hey TARS, switch off the TV please", how: "Quietly, as if someone's asleep" },
  { id: "h5", say: "Remind me to lock the door at eleven", how: "This one mustn't count as a command" },
  { id: "h6", say: "Can you turn on Ari's hatch", how: "A bit faster than usual" },
  { id: "h7", say: "Turn the volume down", how: "From the kitchen" },
  { id: "h8", say: "AC off", how: "" },
];

const VOICE: Prompt[] = [
  { id: "v1", say: "Hey TARS, what's the weather like tomorrow?", how: "" },
  { id: "v2", say: "Remind me to call my mother at six.", how: "" },
  { id: "v3", say: "The quick brown fox jumps over the lazy dog.", how: "" },
  { id: "v4", say: "Hey TARS, turn off the lights in the kitchen.", how: "From across the room" },
  { id: "v5", say: "Add milk, eggs and bread to the shopping list.", how: "" },
  { id: "v6", say: "What time is it in New York right now?", how: "Quietly" },
];

const kind = PARAMS.get("session") === "voice" ? "voice" : "house";
const prompts = kind === "voice" ? VOICE : HOUSE;
const done = Number(PARAMS.get("done") ?? 0);

let sessionNow: Session = {
  kind,
  title: kind === "voice" ? "Your voice" : "House commands",
  person: "Alon",
  about:
    kind === "voice"
      ? "So TARS knows it's you when you talk to it, and can call you by name."
      : "To test which offline recognizer hears the house commands best, before TARS relies on one.",
  after:
    kind === "voice"
      ? "TARS is learning your voice from these now. It will know you from your next request."
      : "The recognizers are compared on the Pi. The results will be on the Status page.",
  prompts,
  takes: prompts.slice(0, done).map((p, n) => ({ id: n + 1, prompt: p.id, seconds: 2.4, problem: null })),
  skipped: [],
  minutes: kind === "voice" ? 4 : 10,
};

export const session = async (): Promise<Session> => {
  await pause(250);
  return sessionNow;
};

const pause = (ms: number) => new Promise(r => setTimeout(r, ms));
let current: { id: number; prompt: string; started: number; stopped: number | null } | null = null;
let count = 100;
const SPEAKS_S = 2.6;

function take(): Take {
  const c = current!;
  const seconds = ((c.stopped ?? Date.now()) - c.started) / 1000;
  return { id: c.id, prompt: c.prompt, seconds, problem: c.id % 4 === 3 ? "quiet" : null };
}

function now(): Recording {
  const c = current!;
  const t = (Date.now() - c.started) / 1000;
  if (c.stopped == null && t > SPEAKS_S + 1.2) c.stopped = Date.now();
  const speaking = t > 0.4 && t < SPEAKS_S;
  const level = c.stopped == null && speaking ? 0.35 + 0.5 * Math.abs(Math.sin(t * 7.3) * Math.cos(t * 2.1)) : 0.03;
  if (c.stopped == null) return { id: c.id, state: "recording", seconds: t, level, take: null };
  const kept = take();
  sessionNow = {
    ...sessionNow,
    takes: [...sessionNow.takes.filter(k => k.prompt !== kept.prompt), kept],
  };
  return { id: c.id, state: "done", seconds: kept.seconds, level: 0, take: kept };
}

export async function record(prompt: string): Promise<Recording> {
  await pause(450); // the beep
  current = { id: ++count, prompt, started: Date.now(), stopped: null };
  return now();
}

export async function poll(_id: number): Promise<Recording> {
  return now();
}

export async function stop(_id: number): Promise<Recording> {
  if (current && current.stopped == null) current.stopped = Date.now();
  await pause(300);
  return now();
}

/** The step ?step= names, at prompt `index`, and the error shown with it; null for the page's own first step. */
export function demoStep(s: Session, index: number): { step: Step; error: string | null } | null {
  const prompt = s.prompts[index].id;
  const heard = (problem: Take["problem"]): Step => ({ at: "heard", take: { id: 7, prompt, seconds: 2.4, problem } });
  switch (PARAMS.get("step")) {
    case "ready":
      return { step: { at: "ready" }, error: null };
    case "recording":
      return {
        step: { at: "recording", rec: { id: 7, state: "recording", seconds: 1.8, level: 0.6, take: null } },
        error: null,
      };
    case "heard":
      return { step: heard(null), error: null };
    case "problem":
      return { step: heard("quiet"), error: null };
    case "busy":
      return {
        step: { at: "ready" },
        error: "TARS is in the middle of a conversation. Try again in a moment.",
      };
    case "done":
      return { step: { at: "done" }, error: null };
    case "gone":
      return { step: { at: "gone", why: "It was used up or it expired: links last a day." }, error: null };
    default:
      return null;
  }
}
