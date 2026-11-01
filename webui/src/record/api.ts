/* The recording session's calls. Its link's token is the only key: the page needs nothing else from the web UI. */
import { getJson, post } from "../api";
import { DEMO } from "../params";
import type { Recording, Session } from "./types";

export const TOKEN = new URLSearchParams(location.search).get("session") ?? "";
const base = () => `/api/session/${encodeURIComponent(TOKEN)}`;

const demo = () => import("./demo");

export const load = async (): Promise<Session> => (DEMO ? (await demo()).session() : getJson<Session>(base()));

/** TARS stops listening for its name while the session is open (so "hey TARS" in a prompt doesn't wake it), until
    `leave`, or a few minutes after the last call. */
export const begin = async () => (DEMO ? undefined : post(`${base()}/begin`));

/** The speakerphone beeps, then records: this answers once it's recording. */
export const record = async (prompt: string): Promise<Recording> =>
  DEMO ? (await demo()).record(prompt) : post<Recording>(`${base()}/record`, { prompt }, 10_000);

export const poll = async (id: number): Promise<Recording> =>
  DEMO ? (await demo()).poll(id) : getJson<Recording>(`${base()}/record/${id}`, { timeoutMs: 5000 });

export const stop = async (id: number): Promise<Recording> =>
  DEMO ? (await demo()).stop(id) : post<Recording>(`${base()}/record/${id}/stop`);

export const skip = async (prompt: string) => (DEMO ? undefined : post(`${base()}/skip`, { prompt }));

/** All done: the link stops working, and what follows the session starts (a voice session becomes a voiceprint). */
export const finish = async () => (DEMO ? undefined : post(`${base()}/finish`));

export const audioUrl = (take: number) => (DEMO ? null : `${base()}/takes/${take}/audio`);

/** Leaving the page gives TARS its ears back now, not when the session times out. */
export function leave() {
  if (!DEMO) navigator.sendBeacon(`${base()}/leave`);
}
