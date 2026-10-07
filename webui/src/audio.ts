/* The playing clip and its progress, in a store of its own so the progress bar repaints every frame without
   re-rendering the page or any other play button. */
import { useSyncExternalStore } from "react";
import { DEMO } from "./params";

export type Clip = { event: number; part: "wake" | "request" } | { turn: number };
const clipKey = (c: Clip) => ("turn" in c ? `turn-${c.turn}` : `${c.event}-${c.part}`);
const clipUrl = (c: Clip) => ("turn" in c ? `/api/audio/turn/${c.turn}` : `/api/audio/${c.event}/${c.part}`);

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
let audio: HTMLAudioElement | null = null;
let frame = 0;
let demoTimer = 0;

export function stopAudio() {
  if (audio) {
    audio.onended = audio.onerror = null; // a late error from the old clip must not stop the next one
    audio.pause();
    audio = null;
  }
  cancelAnimationFrame(frame);
  clearInterval(demoTimer);
  if (playing.key) emit(null, 0);
}

export const showPlaying = (key: string, progress: number, seconds: number | null = null) =>
  emit(key, progress, seconds);

export function play(c: Clip, onError: () => void) {
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
      if (progress >= 1) stopAudio();
    }, 60);
    return;
  }
  const a = (audio = new Audio(clipUrl(c)));
  a.onended = stopAudio;
  a.onerror = () => {
    stopAudio();
    onError();
  };
  a.play().catch(() => {});
  const tick = () => {
    if (audio !== a) return;
    if (a.duration) emit(key, a.currentTime / a.duration, a.duration);
    frame = requestAnimationFrame(tick);
  };
  tick();
}
