/* What's playing, and how far along: its own little store so the progress ring can repaint
   every frame without re-rendering the page. */
import { useSyncExternalStore } from "react";
import { DEMO } from "./params";

/** A wake's clip (what woke it, or the request after it), or what someone said in a conversation. */
export type Clip = { event: number; part: "wake" | "request" } | { turn: number };
export const clipKey = (c: Clip) => ("turn" in c ? `turn-${c.turn}` : `${c.event}-${c.part}`);
const clipUrl = (c: Clip) => ("turn" in c ? `/api/audio/turn/${c.turn}` : `/api/audio/${c.event}/${c.part}`);

let playing: { key: string | null; p: number | null } = { key: null, p: null };
const subs = new Set<() => void>();
const emit = (key: string | null, p: number | null) => {
  playing = { key, p };
  subs.forEach(f => f());
};
function subscribe(f: () => void) {
  subs.add(f);
  return () => {
    subs.delete(f);
  };
}

export function usePlaying(c: Clip): { on: boolean; p: number | null } {
  const s = useSyncExternalStore(subscribe, () => playing);
  return s.key === clipKey(c) ? { on: true, p: s.p } : { on: false, p: null };
}

let audio: HTMLAudioElement | null = null,
  raf = 0,
  fake = 0;

export function stopAudio() {
  if (audio) {
    audio.onended = audio.onerror = null; // a late error from this clip must not stop the next one
    audio.pause();
    audio = null;
  }
  cancelAnimationFrame(raf);
  clearInterval(fake);
  if (playing.key) emit(null, null);
}

/** Shows a clip as playing without playing it (for a state opened from a link). */
export const showPlaying = (key: string, p: number) => emit(key, p);

export function play(c: Clip, onError: () => void) {
  const key = clipKey(c),
    was = playing.key;
  stopAudio();
  if (was === key) return;
  emit(key, 0);
  if (DEMO) {
    let p = 0;
    fake = window.setInterval(() => {
      p += 0.025;
      emit(key, p);
      if (p >= 1) stopAudio();
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
    raf = requestAnimationFrame(tick);
  };
  tick();
}
