/* The recording page's sample session, with no server: ?demo&session=house|voice|wake (wake: recorded on this
   device, as a wake test made with --device is). The take's meter moves, it stops
   by itself after a few seconds as TARS would, and every fourth take comes back too quiet, to show a redo.

   For the screenshot tests: &done=<n> prompts already recorded, and &later=<n> after them left for later, and
   &step=ready|recording|heard|problem|busy|list|end|done|gone
   to open the page at that step. */
import { PARAMS } from "../params";
import type { Step } from "./Record";
import type { Prompt, Recording, Session, Take } from "./types";

const HOUSE: Prompt[] = [
  { id: "h1", say: "Turn on the AC in the bedroom", how: "", seconds: 0, countdown: 0 },
  { id: "h2", say: "Lock the front door", how: "From across the room", seconds: 0, countdown: 0 },
  { id: "h3", say: "Set the living room AC to twenty three", how: "", seconds: 0, countdown: 0 },
  {
    id: "h4",
    say: "Hey TARS, switch off the TV please",
    how: "Quietly, as if someone's asleep",
    seconds: 0,
    countdown: 0,
  },
  {
    id: "h5",
    say: "Remind me to lock the door at eleven",
    how: "This one mustn't count as a command",
    seconds: 0,
    countdown: 0,
  },
  { id: "h6", say: "Can you turn on Ari's hatch", how: "A bit faster than usual", seconds: 0, countdown: 0 },
  { id: "h7", say: "Turn the volume down", how: "From the kitchen", seconds: 0, countdown: 0 },
  { id: "h8", say: "AC off", how: "", seconds: 0, countdown: 0 },
];

const VOICE: Prompt[] = [
  { id: "v1", say: "Hey TARS, what's the weather like tomorrow?", how: "", seconds: 0, countdown: 0 },
  { id: "v2", say: "Remind me to call my mother at six.", how: "", seconds: 0, countdown: 0 },
  { id: "v3", say: "The quick brown fox jumps over the lazy dog.", how: "", seconds: 0, countdown: 0 },
  {
    id: "v4",
    say: "Hey TARS, turn off the lights in the kitchen.",
    how: "From across the room",
    seconds: 0,
    countdown: 0,
  },
  { id: "v5", say: "Add milk, eggs and bread to the shopping list.", how: "", seconds: 0, countdown: 0 },
  { id: "v6", say: "What time is it in New York right now?", how: "Quietly", seconds: 0, countdown: 0 },
];

const WAKE: Prompt[] = [
  { id: "w1", say: "Hey TARS", how: "", seconds: 0, countdown: 0 },
  {
    id: "r1",
    say: "Hey TARS, what's the weather tomorrow?",
    how: "In one breath, as you'd ask",
    seconds: 0,
    countdown: 0,
  },
  { id: "l1", say: "Hey cars", how: "As written: it sounds like TARS, but isn't", seconds: 0, countdown: 0 },
  { id: "w2", say: "Hey TARS", how: "From across the room", seconds: 0, countdown: 4 },
  {
    id: "w3",
    say: "Hey TARS",
    how: "With the TV or music on, as loud as you'd usually have it",
    seconds: 0,
    countdown: 0,
  },
  {
    id: "b1",
    say: "Just the room, for a minute",
    how: "With the TV on, and nobody saying \u201chey TARS\u201d",
    seconds: 60,
    countdown: 0,
  },
  { id: "w4", say: "Hey TARS", how: "While other people are talking nearby", seconds: 0, countdown: 0 },
];

const asked = PARAMS.get("session");
const kind = asked === "voice" || asked === "wake" ? asked : "house";
const prompts = { voice: VOICE, wake: WAKE, house: HOUSE }[kind];
const TITLES = { voice: "Your voice", house: "House commands", wake: "Hey TARS" };
const ABOUT = {
  voice: "So TARS knows it's you when you talk to it, and can call you by name.",
  house: "To test which offline recognizer hears the house commands best, before TARS relies on one.",
  wake:
    "To measure how well TARS hears its name on real microphones, in real rooms. Your recordings stay on TARS and " +
    "are only used to test it: none of them is sent anywhere, or used to train it on your voice.",
};
const AFTER = {
  voice: "TARS is learning your voice from these now. It will know you from your next request.",
  house: "The recognizers are compared on the Pi. The results will be on the Status page.",
  wake: "They're kept on TARS, and played to its wake models to see which one hears its name best.",
};
const done = Number(PARAMS.get("done") ?? 0);
const later = Number(PARAMS.get("later") ?? 0); // the prompts after those done, left for later

let sessionNow: Session = {
  kind,
  title: TITLES[kind],
  mic: kind === "wake" ? "here" : "tars",
  device: kind === "wake" ? "Alon's phone" : "",
  person: "Alon",
  about: ABOUT[kind],
  after: AFTER[kind],
  prompts,
  takes: prompts.slice(0, done).map((p, n) => ({ id: n + 1, prompt: p.id, seconds: 2.4, problem: null })),
  skipped: prompts.slice(done, done + later).map(p => p.id),
  minutes: { voice: 4, house: 10, wake: 14 }[kind],
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
    case "countdown":
      return { step: { at: "countdown", left: 3 }, error: null };
    case "busy":
      return {
        step: { at: "ready" },
        error: "TARS is in the middle of a conversation. Try again in a moment.",
      };
    case "list":
      return { step: { at: "list" }, error: null };
    case "end":
      return { step: { at: "end" }, error: null };
    case "done":
      return { step: { at: "done" }, error: null };
    case "gone":
      return { step: { at: "gone", why: "It was used up or it expired: links last a day." }, error: null };
    default:
      return null;
  }
}

/** A take recorded on this device and uploaded (a wake test): kept as the speakerphone's are. */
export async function upload(prompt: string): Promise<Take> {
  await pause(300);
  const kept: Take = { id: ++count, prompt, seconds: 2.4, problem: count % 4 === 3 ? "quiet" : null };
  sessionNow = { ...sessionNow, takes: [...sessionNow.takes.filter(k => k.prompt !== prompt), kept] };
  return kept;
}
