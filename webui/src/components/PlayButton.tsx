import type { CSSProperties } from "react";
import { type Clip, type Playing, play, useProgress } from "../audio";
import { toast } from "../store";
import { Icon } from "./Icon";

const start = (clip: Clip) => play(clip, () => toast("Couldn't play that recording."));
const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const elapsed = (p: Playing) => (p.seconds ? `${clock(p.progress * p.seconds)} of ${clock(p.seconds)}` : "Playing");

export function PlaySmall({ clip, label }: { clip: Clip; label: string }) {
  const on = useProgress(clip) !== null;
  return (
    <button
      type="button"
      className="play-sm"
      aria-pressed={on}
      aria-label={`${on ? "Stop" : "Play"} ${label}`}
      onClick={e => {
        e.stopPropagation();
        start(clip);
      }}
    >
      <span>
        <Icon name={on ? "pause" : "play"} />
      </span>
    </button>
  );
}

export function PlayButton({ clip, label }: { clip: Clip; label: string }) {
  const on = useProgress(clip) !== null;
  return (
    <button type="button" className="play" aria-label={`${on ? "Stop" : "Play"} ${label}`} onClick={() => start(clip)}>
      <Icon name={on ? "pause" : "play"} />
    </button>
  );
}

export function Player({ clip, label }: { clip: Clip; label: string }) {
  const p = useProgress(clip);
  return (
    <div className="player">
      <button type="button" className="play" aria-label={`${p ? "Stop" : "Play"} ${label}`} onClick={() => start(clip)}>
        <Icon name={p ? "pause" : "play"} />
      </button>
      <span className="bar" style={{ "--p": `${Math.round((p?.progress ?? 0) * 100)}%` } as CSSProperties}>
        <i />
      </span>
      <span className="dur">{p ? elapsed(p) : ""}</span>
    </div>
  );
}
