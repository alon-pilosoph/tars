/* The playing clip and its progress, in a store of its own so the progress ring repaints every frame without
   re-rendering the page or any other play button. */
import { useSyncExternalStore } from "react";
import { DEMO } from "./params";

export type Clip = { event: number; part: "wake" | "request" } | { turn: number };
const clipKey = (c: Clip) => ("turn" in c ? `turn-${c.turn}` : `${c.event}-${c.part}`);
const clipUrl = (c: Clip) => ("turn" in c ? `/api/audio/turn/${c.turn}` : `/api/audio/${c.event}/${c.part}`);

let playing: { key: string | null; progress: number } = { key: null, progress: 0 };
const subs = new Set<() => void>();
function emit(key: string | null, progress: number) {
  playing = { key, progress };
  subs.forEach(f => f());
}
function subscribe(f: () => void) {
  subs.add(f);
  return () => {
    subs.delete(f);
  };
}

export function useProgress(c: Clip): number | null {
  const key = clipKey(c);
  const progress = useSyncExternalStore(subscribe, () => (playing.key === key ? playing.progress : -1));
  return progress < 0 ? null : progress;
}

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

export const showPlaying = (key: string, progress: number) => emit(key, progress);

export function play(c: Clip, onError: () => void) {
  const key = clipKey(c);
  const was = playing.key;
  stopAudio();
  if (was === key) return;
  emit(key, 0);
  if (DEMO) {
    let progress = 0;
    demoTimer = window.setInterval(() => {
      progress += 0.025;
      emit(key, progress);
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
    if (a.duration) emit(key, a.currentTime / a.duration);
    frame = requestAnimationFrame(tick);
  };
  tick();
}
