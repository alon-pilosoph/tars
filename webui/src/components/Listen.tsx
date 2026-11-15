/* Review's Listen: the clips to check, played one after another, each under TARS's guess. A clip that plays to its
   end untouched takes the guess as the answer and the next one starts; tapping the other answer says otherwise. A
   clip with no guess waits for an answer. Only a clip that really played to its end, with the page in view, counts
   as agreeing: hiding the page or a clip that won't play pauses it. */
import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { type Clip, play, stopAudio, useProgress } from "../audio";
import { stamp } from "../format";
import { labelQuietly } from "../store";
import type { Label, TarsEvent } from "../types";
import { Icon } from "./Icon";
import { What } from "./WakeWhat";

const NEXT_MS = 350; // a breath between an agreed clip and the next
const ANSWER: Record<Label, string> = { real: "hey TARS", not_real: "Not it" };

type Phase = "playing" | "waiting" | "paused" | "failed" | "done";
interface Answered {
  id: number;
  label: Label;
  agreed: boolean; // left to the guess, not tapped
}

const clipOf = (e: TarsEvent): Clip => ({ event: e.id, part: "wake" });

/** `queue`: the clips to check, as Review listed them when Listen opened. */
export function Listen({ queue, onClose }: { queue: TarsEvent[]; onClose: () => void }) {
  const [index, setIndex] = useState(0);
  const [phase, setPhase] = useState<Phase>(queue.length ? "playing" : "done");
  const [answered, setAnswered] = useState<Answered[]>([]);
  const next = useRef(0); // the timer to the next clip, after an agreed one
  const at = useRef(0); // the clip whose end is being waited for, against a late end from one left behind
  const e = queue[index] as TarsEvent | undefined;
  const again = useRef<(i: number) => void>(() => {}); // start, for the timer it sets itself

  /** Plays clip `i`; what happens when it ends comes later, from the audio, never during a render or an effect. */
  const playClip = useCallback(
    (i: number) => {
      const ev = queue[i];
      at.current = ev.id;
      play(
        clipOf(ev),
        () => at.current === ev.id && setPhase("failed"),
        () => {
          if (at.current !== ev.id) return;
          if (document.hidden) return setPhase("paused"); // it ended unseen: not an answer
          if (!ev.auto_label) return setPhase("waiting");
          void labelQuietly(ev.id, ev.auto_label);
          setAnswered(a => [...a, { id: ev.id, label: ev.auto_label!, agreed: true }]);
          next.current = window.setTimeout(() => again.current(i + 1), NEXT_MS);
        },
      );
    },
    [queue],
  );

  /** Clip `i` from its start, or the end once there are no more. */
  const start = useCallback(
    (i: number) => {
      clearTimeout(next.current);
      if (i >= queue.length) {
        at.current = 0;
        stopAudio();
        setPhase("done");
        return;
      }
      setIndex(i);
      setPhase("playing");
      playClip(i);
    },
    [queue, playClip],
  );

  useEffect(() => {
    again.current = start;
  }, [start]);

  useEffect(() => {
    if (queue.length) playClip(0);
    return () => {
      clearTimeout(next.current);
      at.current = 0;
      stopAudio();
    };
  }, [queue, playClip]);

  const pause = useCallback(() => {
    clearTimeout(next.current);
    at.current = 0;
    stopAudio();
    setPhase(p => (p === "done" ? p : "paused"));
  }, []);

  useEffect(() => {
    const hidden = () => document.hidden && pause();
    document.addEventListener("visibilitychange", hidden);
    return () => document.removeEventListener("visibilitychange", hidden);
  }, [pause]);

  function answer(label: Label) {
    if (!e) return;
    clearTimeout(next.current);
    at.current = 0;
    stopAudio();
    void labelQuietly(e.id, label);
    setAnswered(a => [...a, { id: e.id, label, agreed: false }]);
    start(index + 1);
  }

  /** The last answer taken back, and its clip again. */
  function back() {
    const last = answered[answered.length - 1];
    if (!last) return;
    clearTimeout(next.current);
    void labelQuietly(last.id, null);
    setAnswered(a => a.slice(0, -1));
    start(queue.findIndex(q => q.id === last.id));
  }

  function close() {
    clearTimeout(next.current);
    at.current = 0;
    stopAudio();
    onClose();
  }

  useEffect(() => {
    const key = (ev: KeyboardEvent) => ev.key === "Escape" && close();
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const total = queue.length;
  const done = phase === "done";
  return (
    <div className="listen" role="dialog" aria-modal="true" aria-label="Listening to the wakes to check">
      <header className="listen-top">
        <span className="listen-count">{done ? `${total} of ${total}` : `${index + 1} of ${total}`}</span>
        <span className="bar" style={{ "--p": `${Math.round((answered.length / total) * 100)}%` } as CSSProperties}>
          <i />
        </span>
        <button className="icon-btn" aria-label="Close" onClick={close}>
          <Icon name="close" />
        </button>
      </header>
      {done || !e ? (
        <Summary answered={answered} onClose={close} />
      ) : (
        <>
          <main className="listen-main" data-id={e.id}>
            <Guess e={e} />
            <div>
              <p className="listen-what">
                <What e={e} />
              </p>
              <div className="wake-when">{stamp(e.ts)}</div>
            </div>
            <ClipBar key={e.id} clip={clipOf(e)} ended={phase === "waiting"} />
            <p className="listen-status" aria-live="polite">
              {STATUS[phase](e)}
            </p>
          </main>
          <footer className="listen-dock">
            <div className="answers listen-answers" role="group" aria-label="Was it really hey TARS?">
              {(["real", "not_real"] as Label[]).map(v => (
                <button
                  key={v}
                  className={`ans answer-${v === "real" ? "yes" : "no"}${e.auto_label === v ? " guess" : ""}`}
                  onClick={() => answer(v)}
                >
                  {ANSWER[v]}
                </button>
              ))}
            </div>
            <div className="listen-controls">
              <button className="link-btn" onClick={back} disabled={!answered.length}>
                <Icon name="undo" size="s" /> Back
              </button>
              {phase === "playing" ? (
                <button className="link-btn" onClick={pause}>
                  <Icon name="pause" size="s" /> Pause
                </button>
              ) : (
                <button className="link-btn" onClick={() => start(index)}>
                  <Icon name="play" size="s" /> {phase === "paused" ? "Carry on" : "Play again"}
                </button>
              )}
            </div>
          </footer>
        </>
      )}
    </div>
  );
}

const STATUS: Record<Exclude<Phase, "done">, (e: TarsEvent) => string> = {
  playing: e =>
    e.auto_label
      ? "Do nothing to agree: the next one starts when this ends. Tap the other answer if it's wrong."
      : "TARS has no guess for this one: answer when it ends.",
  waiting: () => "Which was it?",
  paused: () => "Paused. Nothing is answered while it's paused.",
  failed: () => "This one wouldn't play. Answer it, or go on with Play again.",
};

function Guess({ e }: { e: TarsEvent }) {
  const g = e.auto_label;
  return (
    <div className={`listen-guess ${g === "real" ? "yes" : g === "not_real" ? "no" : "none"}`}>
      <span className="listen-k">{g ? "TARS thinks" : "No guess"}</span>
      <span className="listen-v">{g ? ANSWER[g] : "Your call"}</span>
    </div>
  );
}

const two = (n: number) => String(Math.floor(n)).padStart(2, "0");
const clock = (s: number) => `${two(s / 60)}:${two(s % 60)}`;

/** The clip's progress, and its length all along: before it plays (once known), while it does, and once it ended
    (`ended`, waiting for an answer) or stopped. */
function ClipBar({ clip, ended }: { clip: Clip; ended: boolean }) {
  const p = useProgress(clip);
  const [length, setLength] = useState<number | null>(null);
  if (p?.seconds && p.seconds !== length) setLength(p.seconds); // kept for when it's no longer playing
  const progress = p ? p.progress : ended ? 1 : 0;
  const seconds = p?.seconds ?? length;
  return (
    <div className="listen-clip">
      <span className="bar" style={{ "--p": `${Math.round(progress * 100)}%` } as CSSProperties}>
        <i />
      </span>
      <span className="dur">{seconds ? `${clock(progress * seconds)}/${clock(seconds)}` : "00:00/--:--"}</span>
    </div>
  );
}

function Summary({ answered, onClose }: { answered: Answered[]; onClose: () => void }) {
  const agreed = answered.filter(a => a.agreed).length;
  const yes = answered.filter(a => a.label === "real").length;
  return (
    <main className="listen-main listen-end">
      <span className="rec-done-mark">
        <Icon name="check" size="l" />
      </span>
      <h2 className="listen-title">All listened to</h2>
      <p className="listen-what">
        {answered.length} answered: {agreed} as TARS guessed, {answered.length - agreed} by you.{" "}
        {yes ? `${yes} ${yes === 1 ? "was" : "were"} “hey TARS”.` : "None was “hey TARS”."}
      </p>
      <button className="btn primary big" onClick={onClose}>
        Done
      </button>
    </main>
  );
}
