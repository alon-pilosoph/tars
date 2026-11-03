/* A recording session, opened from the link TARS sends: one prompt at a time, recorded on TARS's speakerphone.
   Tap, the speakerphone beeps, say the words; TARS stops the take when you stop talking (or tap again), checks it,
   and you hear it back or go on. Built for a phone held in one hand: everything you tap is at the bottom. */
import { type CSSProperties, useEffect, useRef, useState } from "react";
import { stopAudio } from "../audio";
import { Icon } from "../components/Icon";
import { Player } from "../components/PlayButton";
import { Toasts } from "../components/Toasts";
import { DEMO } from "../params";
import { audioUrl, begin, finish as finishSession, leave, load, poll, record, skip, stop } from "./api";
import type { Problem, Prompt, Recording, Session, Take } from "./types";

const POLL_MS = 150;

export type Step =
  | { at: "loading" }
  | { at: "gone"; why: string }
  | { at: "intro" }
  | { at: "ready" }
  | { at: "beep" }
  | { at: "recording"; rec: Recording }
  | { at: "checking" }
  | { at: "heard"; take: Take }
  | { at: "done" };

/** What a take's problem means, and what to do about it. */
export const PROBLEMS: Record<Problem, [string, string]> = {
  nothing: ["TARS didn't hear anything.", "Wait for the beep, then say it."],
  quiet: ["That was too quiet.", "Come a little closer, or speak up a bit."],
  loud: ["That was too loud and came out distorted.", "Step back a little."],
  cut_off: ["The start or the end got cut off.", "Start after the beep, and stop a moment before tapping."],
  short: ["That was very short.", "Say the whole phrase."],
};

/** The first prompt with no take that wasn't skipped, or null when they're all done. */
export function nextPrompt(s: Session): number | null {
  const done = new Set([...s.takes.map(t => t.prompt), ...s.skipped]);
  const n = s.prompts.findIndex(p => !done.has(p.id));
  return n < 0 ? null : n;
}

const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export function RecordPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [step, setStep] = useState<Step>({ at: "loading" });
  const [index, setIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const polling = useRef(0);
  const following = useRef<number | null>(null); // the take being polled, until it's done or stopped by hand

  useEffect(() => {
    load()
      .then(async s => {
        const n = nextPrompt(s);
        const shown = DEMO ? (await import("./demo")).demoStep(s, n ?? 0) : null;
        setSession(s);
        setIndex(n ?? 0);
        setStep(shown?.step ?? (n === null ? { at: "done" } : { at: "intro" }));
        setError(shown?.error ?? null);
        if (n === null) finishSession().catch(() => {}); // everything was done before the page closed: close the session
      })
      .catch((e: Error) => setStep({ at: "gone", why: e.message }));
    const away = () => leave();
    window.addEventListener("pagehide", away);
    return () => window.removeEventListener("pagehide", away);
  }, []);

  useEffect(
    () => () => {
      following.current = null;
      clearTimeout(polling.current);
    },
    [],
  );

  // For the screenshot tests, as main.tsx does for the rest of the web UI: loaded, and the fonts in.
  const loaded = step.at !== "loading";
  useEffect(() => {
    if (DEMO && loaded)
      void document.fonts.ready.then(() => Object.assign(document.documentElement.dataset, { ready: "true" }));
  }, [loaded]);

  const prompt: Prompt | undefined = session?.prompts[index];

  function heard(rec: Recording) {
    if (!rec.take) return;
    const take = rec.take;
    setSession(s => s && { ...s, takes: [...s.takes.filter(t => t.prompt !== take.prompt), take] });
    setStep({ at: "heard", take });
  }

  /** Until TARS stops the take itself (or a tap does), its length and loudness, for the meter. */
  function follow(id: number) {
    following.current = id;
    polling.current = window.setTimeout(async () => {
      try {
        const rec = await poll(id);
        if (following.current !== id) return;
        if (rec.state === "done") {
          following.current = null;
          return heard(rec);
        }
        setStep(s => (s.at === "recording" && s.rec.id === id ? { at: "recording", rec } : s));
      } catch {
        /* a missed poll is tried again; stopping by hand still works */
      }
      if (following.current === id) follow(id);
    }, POLL_MS);
  }

  async function start() {
    if (!prompt) return;
    stopAudio();
    setError(null);
    setStep({ at: "beep" });
    try {
      const rec = await record(prompt.id);
      setStep({ at: "recording", rec });
      follow(rec.id);
    } catch (e) {
      setError((e as Error).message);
      setStep({ at: "ready" });
    }
  }

  async function finish(id: number) {
    following.current = null;
    clearTimeout(polling.current);
    setStep({ at: "checking" });
    try {
      heard(await stop(id));
    } catch (e) {
      setError((e as Error).message);
      setStep({ at: "ready" });
    }
  }

  function next(s: Session) {
    stopAudio();
    setError(null);
    const n = nextPrompt(s);
    if (n === null) {
      setStep({ at: "done" });
      finishSession().catch(() => leave()); // it may already be finished; either way TARS listens again
    } else {
      setIndex(n);
      setStep({ at: "ready" });
    }
  }

  async function skipThis() {
    if (!session || !prompt) return;
    const s = { ...session, skipped: [...session.skipped, prompt.id] };
    setSession(s);
    next(s);
    await skip(prompt.id).catch(() => {});
  }

  function go() {
    begin().catch(() => {});
    setStep({ at: "ready" });
  }

  if (step.at === "loading")
    return (
      <Frame>
        <div className="skel rec-skel" aria-busy="true" aria-label="Loading">
          <i className="h" />
          <i style={{ width: "70%" }} />
        </div>
      </Frame>
    );
  if (step.at === "gone")
    return (
      <Frame>
        <section className="rec-end" role="alert">
          <h1 className="rec-title">This link doesn't work anymore</h1>
          <p className="rec-lede">{step.why}</p>
          <p className="rec-lede">Ask TARS for a new one.</p>
        </section>
      </Frame>
    );
  if (!session) return null;
  const total = session.prompts.length;
  const kept = session.takes.length;

  if (step.at === "intro") return <Intro session={session} onStart={go} />;
  if (step.at === "done") return <Done session={session} />;
  if (!prompt) return null;

  const position = index + 1;
  return (
    <Frame title={session.title}>
      <div className="rec-progress">
        <span className="rec-count">
          {position} of {total}
        </span>
        <span className="bar" style={{ "--p": `${Math.round((kept / total) * 100)}%` } as CSSProperties}>
          <i />
        </span>
        {(step.at === "ready" || step.at === "heard") && (
          <button className="link-btn muted rec-skip" onClick={skipThis}>
            Skip
          </button>
        )}
      </div>
      <section className="rec-prompt" aria-labelledby="say">
        <p className="rec-how">{prompt.how ? prompt.how : "In your normal voice"}</p>
        <p id="say" className="rec-say" dir="auto">
          “{prompt.say}”
        </p>
        <Status step={step} />
        {error && (
          <p className="form-error rec-error" role="alert">
            <Icon name="alert" size="s" />
            <span>{error}</span>
          </p>
        )}
      </section>
      <Dock>
        {step.at === "heard" ? (
          <div className="rec-pair">
            <button className={`btn big ${step.take.problem ? "primary" : ""}`} onClick={start}>
              {step.take.problem ? "Try again" : "Redo"}
            </button>
            <button className={`btn big ${step.take.problem ? "" : "primary"}`} onClick={() => next(session)}>
              {step.take.problem ? "Keep it" : nextPrompt(session) === null ? "Finish" : "Next"}
            </button>
          </div>
        ) : (
          <RecordButton step={step} onStart={start} onStop={finish} />
        )}
      </Dock>
    </Frame>
  );
}

function Frame({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div className="rec">
      <header className="rec-top">
        <span className="wordmark">TARS</span>
        <span className="muted">{title ?? "Recording session"}</span>
      </header>
      <main className="rec-main">{children}</main>
      <Toasts />
    </div>
  );
}

function Dock({ children }: { children: React.ReactNode }) {
  return <div className="rec-dock">{children}</div>;
}

function Intro({ session, onStart }: { session: Session; onStart: () => void }) {
  const total = session.prompts.length;
  const done = session.takes.length + session.skipped.length;
  return (
    <Frame>
      <section className="rec-intro" aria-labelledby="title">
        <div className="rec-k">{done ? "Welcome back" : `For ${session.person}`}</div>
        <h1 id="title" className="rec-title">
          {session.title}
        </h1>
        <p className="rec-lede">{session.about}</p>
        <ul className="rec-list">
          <li>
            {done
              ? `${done} of ${total} done, about ${Math.max(1, Math.round((session.minutes * (total - done)) / total))} minutes left.`
              : `${total} phrases, about ${session.minutes} minutes.`}
          </li>
          <li>Stand where you'd normally talk to TARS. Its speakerphone records, not your phone.</li>
          <li>Tap, wait for the beep, then say exactly what's on the screen.</li>
          <li>TARS won't answer anyone until you finish or close this page.</li>
        </ul>
      </section>
      <Dock>
        <button className="btn primary big rec-wide" onClick={onStart}>
          {done ? "Carry on" : "Start"}
        </button>
      </Dock>
    </Frame>
  );
}

function Done({ session }: { session: Session }) {
  const kept = session.takes.length;
  const skipped = session.skipped.length;
  return (
    <Frame title={session.title}>
      <section className="rec-end" aria-labelledby="title">
        <span className="rec-done-mark">
          <Icon name="check" size="l" />
        </span>
        <h1 id="title" className="rec-title">
          All done
        </h1>
        <p className="rec-lede">
          {kept} {kept === 1 ? "recording" : "recordings"} saved{skipped ? `, ${skipped} skipped` : ""}. {session.after}
        </p>
        <p className="rec-lede muted">TARS is listening again. You can close this page.</p>
      </section>
    </Frame>
  );
}

function Status({ step }: { step: Step }) {
  let line: React.ReactNode = null;
  if (step.at === "ready") line = <span className="muted">Tap the button, wait for the beep, then say it.</span>;
  else if (step.at === "beep") line = <span className="muted">Listen for the beep…</span>;
  else if (step.at === "recording")
    line = <span className="rec-live">Recording. TARS stops when you do, or tap to stop.</span>;
  else if (step.at === "checking") line = <span className="muted">Checking…</span>;
  else if (step.at === "heard") {
    const { take } = step;
    const problem = take.problem && PROBLEMS[take.problem];
    line = (
      <>
        {problem ? (
          <span className="rec-verdict bad">
            <Icon name="alert" size="s" />
            <span>
              {problem[0]} <span className="muted">{problem[1]}</span>
            </span>
          </span>
        ) : (
          <span className="rec-verdict">
            <Icon name="check" size="s" />
            <span>Sounds good.</span>
          </span>
        )}
        {take.problem !== "nothing" && <Player clip={{ take: take.id, url: audioUrl(take.id) }} label="this take" />}
      </>
    );
  }
  return (
    <div className="rec-status" aria-live="polite">
      {line}
    </div>
  );
}

function RecordButton({ step, onStart, onStop }: { step: Step; onStart: () => void; onStop: (id: number) => void }) {
  const on = step.at === "recording";
  const busy = step.at === "beep" || step.at === "checking";
  const level = on ? step.rec.level : 0;
  return (
    <div className="rec-btn-wrap">
      <button
        className="rec-btn"
        aria-pressed={on}
        disabled={busy}
        aria-label={on ? "Stop recording" : "Record"}
        style={{ "--lvl": level.toFixed(2) } as CSSProperties}
        onClick={() => (on ? onStop(step.rec.id) : onStart())}
      >
        <Icon name={on ? "stop" : "mic"} size="l" />
      </button>
      <span className="rec-btn-label" aria-hidden="true">
        {on ? clock(step.rec.seconds) : step.at === "beep" ? "Beep…" : step.at === "checking" ? "Checking…" : "Record"}
      </span>
    </div>
  );
}
