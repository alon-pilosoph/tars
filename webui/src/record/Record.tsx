/* A recording session, opened from the link TARS sends: one prompt at a time, recorded on TARS's speakerphone, or,
   for a wake test made for a device, on the device the page is open on (here.ts). Tap, a beep, say the words; the
   take stops when you stop talking (or tap again), TARS checks it, and you hear it back or go on. A phrase can be left
   for later, any phrase can be gone back to from the list of them all (heard again, redone), and the session ends only
   when the person taps Finish. Built for a phone held in one hand: everything you tap is at the bottom. */
import { type CSSProperties, useEffect, useRef, useState } from "react";
import { stopAudio } from "../audio";
import { Icon } from "../components/Icon";
import { Player } from "../components/PlayButton";
import { Toasts } from "../components/Toasts";
import { DEMO } from "../params";
import { audioUrl, begin, finish as finishSession, leave, load, poll, record, skip, stop, upload } from "./api";
import { DeviceMic, type Take as DeviceTake } from "./here";
import type { Problem, Prompt, Recording, Session, Take } from "./types";

const POLL_MS = 150;

export type Step =
  | { at: "loading" }
  | { at: "gone"; why: string }
  | { at: "intro" }
  | { at: "ready" }
  | { at: "countdown"; left: number }
  | { at: "beep" }
  | { at: "recording"; rec: Recording }
  | { at: "checking" }
  | { at: "heard"; take: Take }
  | { at: "list" }
  | { at: "end" }
  | { at: "done" };

/** What a take's problem means, and what to do about it. */
export const PROBLEMS: Record<Problem, [string, string]> = {
  nothing: ["TARS didn't hear anything.", "Wait for the beep, then say it."],
  quiet: ["That was too quiet.", "Come a little closer, or speak up a bit."],
  loud: ["That was too loud and came out distorted.", "Step back a little."],
  cut_off: ["The start or the end got cut off.", "Start after the beep, and stop a moment before tapping."],
  short: ["That was very short.", "Say the whole phrase."],
};

/** The first prompt with no take that wasn't left for later, or null when there are none. */
export function nextPrompt(s: Session): number | null {
  const done = new Set([...s.takes.map(t => t.prompt), ...s.skipped]);
  const n = s.prompts.findIndex(p => !done.has(p.id));
  return n < 0 ? null : n;
}

/** Prompts left for later that still have no take, in order. */
export function laterPrompts(s: Session): number[] {
  const recorded = new Set(s.takes.map(t => t.prompt));
  return s.prompts.flatMap((p, n) => (s.skipped.includes(p.id) && !recorded.has(p.id) ? [n] : []));
}

export type PromptState = "recorded" | "redo" | "later" | "todo";
export function promptState(s: Session, p: Prompt): PromptState {
  const take = s.takes.find(t => t.prompt === p.id);
  if (take) return take.problem ? "redo" : "recorded";
  return s.skipped.includes(p.id) ? "later" : "todo";
}

const minutes = (n: number) => `${n} ${n === 1 ? "minute" : "minutes"}`;
const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export function RecordPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [step, setStep] = useState<Step>({ at: "loading" });
  const [index, setIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const polling = useRef(0);
  const following = useRef<number | null>(null); // the take being polled, until it's done or stopped by hand
  const mic = useRef<DeviceMic | null>(null); // this device's microphone, for a session recorded here
  const onDevice = useRef<DeviceTake | null>(null); // the take it's recording
  const takes = useRef(0);
  const counting = useRef(false); // a countdown is on; a tap sets it false to call it off

  useEffect(() => {
    load()
      .then(async s => {
        const n = nextPrompt(s);
        const shown = DEMO ? (await import("./demo")).demoStep(s, n ?? 0) : null;
        setSession(s);
        setIndex(n ?? 0);
        setStep(shown?.step ?? { at: "intro" }); // with nothing left, Carry on goes to the end, to finish
        setError(shown?.error ?? null);
      })
      .catch((e: Error) => setStep({ at: "gone", why: e.message }));
    const away = () => {
      leave();
      mic.current?.close();
      mic.current = null;
    };
    window.addEventListener("pagehide", away);
    return () => {
      window.removeEventListener("pagehide", away);
      mic.current?.close();
    };
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
    if (rec.take) keep(rec.take);
  }

  function keep(take: Take) {
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
    if (session?.mic === "here" && !DEMO) return startHere(prompt);
    try {
      const rec = await record(prompt.id);
      setStep({ at: "recording", rec });
      follow(rec.id);
    } catch (e) {
      setError((e as Error).message);
      setStep({ at: "ready" });
    }
  }

  /** A take on this device's microphone: its beep, until a pause (or its set time, or a tap), then uploaded. */
  async function startHere(p: Prompt) {
    try {
      mic.current ??= new DeviceMic();
      await mic.current.open();
      if (p.countdown && !(await countDown(mic.current, p.countdown))) return setStep({ at: "ready" });
      await mic.current.beep();
      const id = ++takes.current;
      const take = mic.current.record(p.seconds, (level, seconds) =>
        setStep(s =>
          s.at === "recording" && s.rec.id === id ? { at: "recording", rec: { ...s.rec, level, seconds } } : s,
        ),
      );
      onDevice.current = take;
      setStep({ at: "recording", rec: { id, state: "recording", seconds: 0, level: 0, take: null } });
      const { wav, stopped } = await take.done;
      onDevice.current = null;
      setStep({ at: "checking" });
      keep(await upload(p.id, wav, stopped));
    } catch (e) {
      onDevice.current = null;
      setError((e as Error).message);
      setStep({ at: "ready" });
    }
  }

  /** A chime a second while the device is put down and you walk away; false if a tap called it off. */
  async function countDown(device: DeviceMic, seconds: number): Promise<boolean> {
    counting.current = true;
    for (let left = Math.round(seconds); left > 0; left--) {
      if (!counting.current) return false;
      setStep({ at: "countdown", left });
      device.chime();
      await new Promise(r => setTimeout(r, 1000));
    }
    const going = counting.current;
    counting.current = false;
    return going;
  }

  async function finish(id: number) {
    if (onDevice.current) return onDevice.current.stop(); // startHere carries on from there
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

  /** The next phrase not yet done, or the end, where what was left for later is offered before Finish. */
  function next(s: Session) {
    stopAudio();
    setError(null);
    // Doing what was left for later, the one after; otherwise the first not yet done.
    const n = nextPrompt(s) ?? laterPrompts(s).find(i => i > index) ?? null;
    if (n === null) setStep({ at: "end" });
    else {
      setIndex(n);
      setStep({ at: "ready" });
    }
  }

  /** Any phrase, from the list or the end: its take to hear and redo, or ready to record. */
  function goTo(n: number) {
    if (!session) return;
    stopAudio();
    setError(null);
    setIndex(n);
    const take = session.takes.find(t => t.prompt === session.prompts[n].id);
    setStep(take ? { at: "heard", take } : { at: "ready" });
  }

  async function later() {
    if (!session || !prompt) return;
    const s = { ...session, skipped: [...session.skipped.filter(id => id !== prompt.id), prompt.id] };
    setSession(s);
    next(s);
    await skip(prompt.id).catch(() => {});
  }

  async function finishAll() {
    setStep({ at: "done" });
    await finishSession().catch(() => leave()); // it may already be finished; either way TARS listens again
  }

  function go() {
    begin().catch(() => {});
    if (session && nextPrompt(session) === null) return setStep({ at: "end" });
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
  if (step.at === "list")
    return (
      <PromptList
        session={session}
        current={index}
        onPick={goTo}
        onClose={() =>
          nextPrompt(session) === null && !session.takes.some(t => t.prompt === prompt?.id)
            ? setStep({ at: "end" })
            : goTo(index)
        }
      />
    );
  if (step.at === "end")
    return <End session={session} onLater={goTo} onList={() => setStep({ at: "list" })} onFinish={finishAll} />;
  if (!prompt) return null;

  const position = index + 1;
  const idle = step.at === "ready" || step.at === "heard";
  return (
    <Frame title={session.title}>
      <div className="rec-progress">
        <button
          className="link-btn rec-count"
          onClick={() => setStep({ at: "list" })}
          disabled={!idle}
          aria-label={`Phrase ${position} of ${total}: all phrases`}
        >
          {position} of {total}
          <Icon name="chev-down" size="s" />
        </button>
        <span className="bar" style={{ "--p": `${Math.round((kept / total) * 100)}%` } as CSSProperties}>
          <i />
        </span>
        {step.at === "ready" && (
          <button className="link-btn muted rec-skip" onClick={later}>
            Later
          </button>
        )}
      </div>
      <section className="rec-prompt" aria-labelledby="say">
        <p className="rec-how">{prompt.how ? prompt.how : "In your normal voice"}</p>
        <p id="say" className="rec-say" dir="auto">
          {prompt.seconds ? prompt.say : `“${prompt.say}”`}
        </p>
        <Status step={step} timed={prompt.seconds} away={prompt.countdown > 0} />
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
              {step.take.problem ? "Keep it" : "Next"}
            </button>
          </div>
        ) : (
          <RecordButton
            step={step}
            timed={prompt.seconds}
            onStart={start}
            onStop={finish}
            onCancel={() => (counting.current = false)}
          />
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
  const done = session.takes.length;
  const left = laterPrompts(session).length;
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
            {!done
              ? `${total} phrases, about ${session.minutes} minutes.`
              : done === total
                ? `All ${total} recorded. Carry on to finish, or hear and redo any of them first.`
                : `${done} of ${total} recorded${left ? `, ${left} left for later` : ""}, about ${minutes(Math.max(1, Math.round((session.minutes * (total - done)) / total)))} to go.`}
          </li>
          {session.mic === "here" ? (
            <li>
              This {session.device ? `device (${session.device})` : "device"} records, not TARS. Put it where TARS could
              stand, and talk to it from where you'd talk to TARS.
            </li>
          ) : (
            <li>Stand where you'd normally talk to TARS. Its speakerphone records, not your phone.</li>
          )}
          <li>Tap, wait for the beep, then say exactly what's on the screen.</li>
          <li>
            Not the right moment for one (no TV on, nobody around)? Leave it for later. Tap the phrase count at the top
            for all of them, to hear or redo any. This link works for {session.kind === "wake" ? "a week" : "a day"}, or
            until you tap Finish.
          </li>
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
  const skipped = laterPrompts(session).length;
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
          {kept} {kept === 1 ? "recording" : "recordings"} saved{skipped ? `, ${skipped} left out` : ""}.{" "}
          {session.after}
        </p>
        <p className="rec-lede muted">TARS is listening again. You can close this page.</p>
      </section>
    </Frame>
  );
}

const STATE: Record<PromptState, string> = { recorded: "Recorded", redo: "Worth redoing", later: "Later", todo: "" };

/** Every phrase, grouped as they're said (close, from afar, over the TV…), each with how it stands. */
function PromptList({
  session,
  current,
  onPick,
  onClose,
}: {
  session: Session;
  current: number;
  onPick: (n: number) => void;
  onClose: () => void;
}) {
  const groups: { how: string; items: number[] }[] = [];
  session.prompts.forEach((p, n) => {
    const last = groups[groups.length - 1];
    if (last && last.how === p.how) last.items.push(n);
    else groups.push({ how: p.how, items: [n] });
  });
  const kept = session.takes.length;
  return (
    <Frame title={session.title}>
      <div className="rec-list-head">
        <h1 className="rec-title">All phrases</h1>
        <p className="rec-lede muted">
          {kept} of {session.prompts.length} recorded. Tap one to hear it, redo it or record it.
        </p>
      </div>
      <div className="rec-phrases">
        {groups.map((g, i) => (
          <section key={i} className="rec-group" aria-label={g.how || "In your normal voice"}>
            <h2 className="rec-group-how">{g.how || "In your normal voice"}</h2>
            <ol>
              {g.items.map(n => {
                const p = session.prompts[n];
                const state = promptState(session, p);
                return (
                  <li key={p.id}>
                    <button
                      className="rec-phrase"
                      aria-current={n === current ? "step" : undefined}
                      onClick={() => onPick(n)}
                    >
                      <span className="rec-phrase-n">{n + 1}</span>
                      <span className="rec-phrase-say" dir="auto">
                        {p.say}
                      </span>
                      <span className={`rec-phrase-state ${state}`}>
                        {state === "recorded" && <Icon name="check" size="s" />}
                        {state === "redo" && <Icon name="alert" size="s" />}
                        {STATE[state]}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
          </section>
        ))}
      </div>
      <Dock>
        <button className="btn big rec-wide" onClick={onClose}>
          Back
        </button>
      </Dock>
    </Frame>
  );
}

/** The last phrase done: what was left for later, offered before Finish closes the link. */
function End({
  session,
  onLater,
  onList,
  onFinish,
}: {
  session: Session;
  onLater: (n: number) => void;
  onList: () => void;
  onFinish: () => void;
}) {
  const left = laterPrompts(session);
  const total = session.prompts.length;
  return (
    <Frame title={session.title}>
      <section className="rec-end" aria-labelledby="title">
        <h1 id="title" className="rec-title">
          {left.length ? `${left.length} left for later` : "All recorded"}
        </h1>
        <p className="rec-lede">
          {session.takes.length} of {total} recorded.{" "}
          {left.length
            ? "Do them now, or come back to this link when the moment's right: it keeps everything so far."
            : "Hear or redo any of them from the list, or finish."}
        </p>
        <button className="link-btn" onClick={onList}>
          All phrases
        </button>
      </section>
      <Dock>
        <div className="rec-pair">
          {left.length ? (
            <button className="btn big primary" onClick={() => onLater(left[0])}>
              {`Do the ${left.length} now`}
            </button>
          ) : null}
          <button className={`btn big ${left.length ? "" : "primary"}`} onClick={onFinish}>
            {left.length ? "Finish without them" : "Finish"}
          </button>
        </div>
      </Dock>
    </Frame>
  );
}

function Status({ step, timed, away }: { step: Step; timed: number; away: boolean }) {
  let line: React.ReactNode = null;
  if (step.at === "ready")
    line = (
      <span className="muted">
        {timed
          ? "Tap the button, then let the room play."
          : away
            ? "Tap the button, put this down and go across the room. Four chimes, then a beep: say it after the beep."
            : "Tap the button, wait for the beep, then say it."}
      </span>
    );
  else if (step.at === "countdown")
    line = <span className="rec-live">Put it down and go across the room. Say it after the beep.</span>;
  else if (step.at === "beep") line = <span className="muted">Listen for the beep…</span>;
  else if (step.at === "recording")
    line = (
      <span className="rec-live">
        {timed ? "Recording the room. It stops by itself." : "Recording. TARS stops when you do, or tap to stop."}
      </span>
    );
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

function RecordButton({
  step,
  timed,
  onStart,
  onStop,
  onCancel,
}: {
  step: Step;
  timed: number;
  onStart: () => void;
  onStop: (id: number) => void;
  onCancel: () => void;
}) {
  if (step.at === "countdown")
    return (
      <div className="rec-btn-wrap">
        <button className="rec-btn" aria-label="Cancel the countdown" onClick={onCancel}>
          <span className="rec-countdown" aria-live="assertive">
            {step.left}
          </span>
        </button>
        <span className="rec-btn-label" aria-hidden="true">
          Tap to cancel
        </span>
      </div>
    );
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
        {on
          ? clock(timed ? Math.max(0, timed - step.rec.seconds) : step.rec.seconds)
          : step.at === "beep"
            ? "Beep…"
            : step.at === "checking"
              ? "Checking…"
              : "Record"}
      </span>
    </div>
  );
}
