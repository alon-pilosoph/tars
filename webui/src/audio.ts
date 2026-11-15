/* The playing clip and its progress, in a store of its own so the progress bar repaints every frame without
   re-rendering the page or any other play button. */
import { useSyncExternalStore } from "react";
import { DEMO } from "./params";

/** A wake's or request's audio, a conversation turn's, or a take in a recording session (at its own address). */
export type Clip =
  { event: number; part: "wake" | "request" } | { turn: number } | { take: number; url: string | null };
const clipKey = (c: Clip) => ("take" in c ? `take-${c.take}` : "turn" in c ? `turn-${c.turn}` : `${c.event}-${c.part}`);
const clipUrl = (c: Clip) =>
  "take" in c ? (c.url ?? "") : "turn" in c ? `/api/audio/turn/${c.turn}` : `/api/audio/${c.event}/${c.part}`;

export interface Playing {
  progress: number;
  seconds: number | null;
}
let playing: { key: string | null } & Playing = { key: null, progress: 0, seconds: null };
const subs = new Set<() => void>();
function emit(key: string | null, progress: number, seconds: number | null = null) {
  playing = { key, progress, seconds };
  subs.forEach(f => f());
}
function subscribe(f: () => void) {
  subs.add(f);
  return () => {
    subs.delete(f);
  };
}

const NOT_PLAYING = null;
export function useProgress(c: Clip): Playing | null {
  const key = clipKey(c);
  return useSyncExternalStore(subscribe, () => (playing.key === key ? playing : NOT_PLAYING));
}

const DEMO_SECONDS = 3;
// One element for every clip: a phone lets a page play audio only from a tap, but an element a tap has started may
// go on to play other clips, as Review's Listen does one after another.
let element: HTMLAudioElement | null = null;
let turn = 0; // the clip playing now: a late event from one before it is ignored
let frame = 0;
let demoTimer = 0;

export function stopAudio() {
  turn++;
  if (element) {
    element.onended = element.onerror = null; // a late error from the old clip must not stop the next one
    element.pause();
  }
  cancelAnimationFrame(frame);
  clearInterval(demoTimer);
  if (playing.key) emit(null, 0);
}

export const showPlaying = (key: string, progress: number, seconds: number | null = null) =>
  emit(key, progress, seconds);

/** `onEnd`: called once the clip has played to its end (not when it's stopped, or another starts). */
export function play(c: Clip, onError: () => void, onEnd?: () => void) {
  const key = clipKey(c);
  const was = playing.key;
  stopAudio();
  if (was === key) return;
  emit(key, 0);
  if (DEMO) {
    let progress = 0;
    demoTimer = window.setInterval(() => {
      progress += 0.02;
      emit(key, progress, DEMO_SECONDS);
      if (progress >= 1) {
        stopAudio();
        onEnd?.();
      }
    }, 60);
    return;
  }
  const a = (element ??= new Audio());
  const mine = turn;
  a.src = clipUrl(c);
  a.onended = () => {
    if (turn !== mine) return;
    stopAudio();
    onEnd?.();
  };
  const failed = () => {
    if (turn !== mine) return;
    stopAudio();
    onError();
  };
  a.onerror = failed;
  // Refused (the browser wants a tap first) is a failure; aborted (another clip took over) isn't.
  a.play().catch((e: unknown) => {
    if (!(e instanceof DOMException && e.name === "AbortError")) failed();
  });
  const tick = () => {
    if (turn !== mine) return;
    if (a.duration) emit(key, a.currentTime / a.duration, a.duration);
    frame = requestAnimationFrame(tick);
  };
  tick();
}
