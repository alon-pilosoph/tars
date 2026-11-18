/* Talk: TARS from this page, on a phone or a laptop (docs/web-ui.md#talk). A tap opens the microphone and TARS
   answers aloud here; the conversation's words show as it goes. The page is a body of TARS's for as long as it's
   open (docs/bodies.md#the-web-page), let in by a one-time ticket for whoever's signed in. */
import { useEffect, useRef, useState } from "react";
import { DEMO } from "../params";
import { DEMO_LINK } from "../demoParams";
import { personName } from "../format";
import { audioContext, cantTalk, connect } from "../talk/browser";
import type { Line, PageBody, TalkState } from "../talk/protocol";
import { PageHead } from "./Blocks";
import { Icon } from "./Icon";

const FOLLOW_PX = 120; // this close to the bottom, new lines are followed

type Problem = "insecure" | "unsupported" | "refused" | "unreachable" | null;

const LABEL: Record<TalkState, string> = {
  connecting: "Connecting…",
  ready: "Tap to talk",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Tap to stop",
  gone: "Tap to try again",
};

const SAYS: Record<Exclude<Problem, null>, { title: string; text: string }> = {
  insecure: {
    title: "This page needs HTTPS for the microphone",
    text: "Browsers only open the microphone on a secure page. Open TARS by its https:// address, or set [web] tls_cert on the Pi.",
  },
  unsupported: {
    title: "This browser can't record here",
    text: "Try a recent Safari, Chrome or Firefox.",
  },
  refused: {
    title: "The microphone is blocked",
    text: "Allow the microphone for this page in the browser's site settings, then tap again.",
  },
  unreachable: {
    title: "Can't reach TARS's voice",
    text: "The voice assistant on the Pi isn't answering. It may be restarting; try again in a minute.",
  },
};

export function Talk() {
  const demo = DEMO ? demoTalk() : null;
  const [state, setState] = useState<TalkState>(demo?.state ?? "connecting");
  const [lines, setLines] = useState<Line[]>(demo?.lines ?? []);
  const [problem, setProblem] = useState<Problem>(demo?.problem ?? (DEMO ? null : (cantTalk() as Problem)));
  const [person, setPerson] = useState<string | null>(demo?.person ?? null);
  const link = useRef<{ body: PageBody; close: () => void } | null>(null);
  const end = useRef<HTMLDivElement>(null);

  const [attempt, setAttempt] = useState(0); // each one a fresh ticket and connection

  // The body link, open while the page is: a fresh one for each attempt (a tap after it dropped).
  useEffect(() => {
    if (DEMO || cantTalk()) return;
    let made: { body: PageBody; close: () => void } | null = null;
    let live = true;
    connect({
      state: next => {
        setState(next);
        setProblem(p => (next === "gone" ? "unreachable" : p === "unreachable" ? null : p));
      },
      line: line => setLines(all => [...all.slice(-40), line]),
      refused: () => setProblem("refused"),
    })
      .then(opened => {
        if (!live) return opened.close();
        made = link.current = opened;
        setPerson(opened.person);
      })
      .catch(() => {
        if (!live) return;
        setState("gone");
        setProblem("unreachable");
      });
    return () => {
      live = false;
      made?.close();
      link.current = null;
    };
  }, [attempt]);

  // The newest line in view as it comes, above the button, unless you've scrolled up to read: then it's left alone.
  const following = useRef(true);
  useEffect(() => {
    const near = () => {
      following.current = innerHeight + scrollY >= document.documentElement.scrollHeight - FOLLOW_PX;
    };
    addEventListener("scroll", near, { passive: true });
    return () => removeEventListener("scroll", near);
  }, []);
  // Room below the conversation for the button over it, only once the conversation reaches down that far: a short one
  // shouldn't make the page scroll.
  const box = useRef<HTMLDivElement>(null);
  const dock = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const fit = () => {
      if (!box.current || !end.current || !dock.current) return;
      const bottom = end.current.getBoundingClientRect().top + scrollY;
      box.current.classList.toggle("reaches", bottom > dock.current.getBoundingClientRect().top);
    };
    fit();
    if (following.current) end.current?.scrollIntoView?.({ block: "end" });
    addEventListener("resize", fit);
    return () => removeEventListener("resize", fit);
  }, [lines.length, problem]);

  const tap = () => {
    if (DEMO) return;
    audioContext(); // in the tap itself: phones make sound only after one
    if (state === "gone") {
      setState("connecting");
      return setAttempt(a => a + 1);
    }
    if (problem === "refused") setProblem(null);
    void link.current?.body.tap();
  };

  const blocked = problem === "insecure" || problem === "unsupported";
  const busy = state === "connecting" || state === "thinking";
  return (
    <section className="talk">
      <PageHead
        title="Talk"
        sub={person ? `TARS knows it's you, ${personName(person)}, from your sign-in.` : "Talk to TARS from here."}
      />
      {problem && (
        <div className="talk-problem" role="alert">
          <Icon name="alert" />
          <div>
            <b>{SAYS[problem].title}</b>
            <p>{SAYS[problem].text}</p>
          </div>
        </div>
      )}
      <div className="talk-lines" aria-live="polite" ref={box}>
        {lines.length === 0 && !blocked && (
          <p className="talk-hint">
            Tap the button and ask anything, as you would aloud at home. Tap again to stop TARS while it talks.
          </p>
        )}
        {lines.map((line, i) => (
          <p key={i} className={`talk-line ${line.who}`}>
            <span className="talk-who">{line.who === "you" ? "You" : "TARS"}</span>
            {line.text}
          </p>
        ))}
        <div ref={end} className="talk-end" />
      </div>
      <div className="talk-dock" ref={dock}>
        <button
          className={`talk-button ${state}`}
          onClick={tap}
          disabled={blocked || busy}
          aria-label={blocked ? "Talking isn't possible here" : LABEL[state]}
          aria-pressed={state === "listening"}
        >
          <span className="talk-ring" aria-hidden="true" />
          <Icon name={state === "speaking" ? "stop" : "mic"} />
        </button>
        <p className="talk-state" aria-hidden="true">
          {blocked ? "Not available here" : LABEL[state]}
        </p>
      </div>
    </section>
  );
}

/** The screenshot tests' states: ?demo&tab=talk&talk=listening|speaking|long|insecure|refused|gone, or none (ready). */
function demoTalk(): { state: TalkState; lines: Line[]; problem: Problem; person: string | null } {
  const lines: Line[] = [
    { who: "you", text: "What's the weather tomorrow?" },
    { who: "tars", text: "Sunny and 24 in the afternoon, with a breeze from the sea." },
    { who: "you", text: "And should I take a jacket in the evening?" },
  ];
  const talk = DEMO_LINK.talk;
  if (talk === "listening") return { state: "listening", lines, problem: null, person: "alon" };
  if (talk === "speaking")
    return {
      state: "speaking",
      lines: [...lines, { who: "tars", text: "A light one: it drops to 17 after sunset." }],
      problem: null,
      person: "alon",
    };
  if (talk === "insecure") return { state: "ready", lines: [], problem: "insecure", person: null };
  if (talk === "refused") return { state: "ready", lines: [], problem: "refused", person: "alon" };
  if (talk === "long") {
    const many: Line[] = Array.from({ length: 8 }, () => lines.slice(0, 2)).flat(); // asked, answered, and again
    return {
      state: "listening",
      lines: [...many, { who: "tars", text: "And that's the last thing said." }],
      problem: null,
      person: "alon",
    };
  }
  if (talk === "gone") return { state: "gone", lines, problem: "unreachable", person: "alon" };
  return { state: "ready", lines: [], problem: null, person: "alon" };
}
