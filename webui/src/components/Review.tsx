import { heardText, stamp } from "../format";
import { type State, answerWake, clusterName, menuOpen, reviewEvents, toggleMenu, useStore } from "../store";
import type { Label, TarsEvent } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";
import { PlayButton } from "./PlayButton";

export function Review() {
  const s = useStore();
  // Rows stay where they are until Refresh, even after an answer is given or cleared.
  const all = reviewEvents(s);
  const toCheck = all.filter(e => s.snapshot.review.has(e.id));
  const reviewed = all.filter(e => !s.snapshot.review.has(e.id));
  const left = toCheck.filter(e => !e.label).length;
  const head = (
    <PageHead
      title={left ? `${left} to check` : "Review"}
      sub="TARS wasn't sure these were meant for it. Its guess is shaded. Answers move to Reviewed at the next Refresh."
    />
  );
  if (!all.length)
    return (
      <>
        {head}
        <Empty
          title="Nothing to check"
          text="When TARS isn't sure it was called, the clip lands here for a second opinion."
        />
      </>
    );
  return (
    <>
      {head}
      {toCheck.length ? (
        <div className="wakes">
          {toCheck.map(e => (
            <Wake key={e.id} e={e} s={s} />
          ))}
        </div>
      ) : (
        <Empty title="All checked" text="Nothing to listen to until TARS has another doubt." />
      )}
      {reviewed.length > 0 && (
        <section className="reviewed">
          <SecHead title="Reviewed">
            <span className="muted">Tap the other answer to change it.</span>
          </SecHead>
          <div className="wakes">
            {reviewed.map(e => (
              <Wake key={e.id} e={e} s={s} reviewed />
            ))}
          </div>
        </section>
      )}
    </>
  );
}

function What({ e }: { e: TarsEvent }) {
  if (e.kind === "near_miss") return <>Almost woke, then didn't</>;
  const h = <span className="heard">{`“${heardText(e.heard || "")}”`}</span>;
  if (e.outcome === "ignore") return <>Heard {h} and stayed quiet</>;
  if (e.outcome === "ask") return <>Heard {h} and asked “Did you call me?”</>;
  if (e.follow === "said_nothing") return <>Woke on {h}, but nobody spoke</>;
  if (e.follow === "not_for_us") return <>Woke on {h} and heard this</>;
  return <>Woke on {h}</>;
}

const n2 = (x: number | null) => (x == null ? "?" : x.toFixed(2));

function score(e: TarsEvent, s: State) {
  if (e.kind === "near_miss") {
    const threshold = s.models?.active?.threshold;
    return `Score ${n2(e.wake_score)}${threshold != null ? ` of ${threshold}` : ""}.`;
  }
  const sure = e.confidence != null ? `${Math.round(e.confidence * 100)}% sure, ` : "";
  return `${sure}score ${n2(e.wake_score)}.`;
}

const capital = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);
const GUESS = { real: "TARS thinks yes.", not_real: "TARS thinks no." };

function Wake({ e, s, reviewed }: { e: TarsEvent; s: State; reviewed?: boolean }) {
  const guess = !e.label && e.auto_label;
  const voice = clusterName(s, e.cluster_id) || "No voice";
  const menu = { kind: "event", id: e.id } as const;
  const chip = { kind: "eventVoice", id: e.id } as const;
  const answer = (v: Label, text: string) => (
    <button
      className={`ans answer-${v === "real" ? "yes" : "no"}${guess === v ? " guess" : ""}`}
      aria-pressed={e.label === v}
      onClick={() => answerWake(e.id, v)}
    >
      {e.label === v && <Icon name="check" size="s" />}
      {text}
    </button>
  );
  return (
    <article className="wake" data-id={e.id} aria-label={`Wake, ${stamp(e.ts)}`}>
      <div className="wake-head">
        {e.has_wake_audio ? <PlayButton clip={{ event: e.id, part: "wake" }} label="what woke it" /> : <span />}
        <div>
          <div className="wake-what">
            <What e={e} />
          </div>
          <div className="wake-when">{stamp(e.ts)}</div>
        </div>
        <div className="anchor">
          <button
            className="icon-btn"
            aria-haspopup="menu"
            aria-expanded={menuOpen(s, menu)}
            aria-label="More for this wake"
            onClick={ev => toggleMenu(menu, ev.currentTarget)}
          >
            <Icon name="more" />
          </button>
        </div>
      </div>
      {e.has_request_audio || e.transcript ? (
        <div className="reply">
          {e.has_request_audio ? <PlayButton clip={{ event: e.id, part: "request" }} label="the reply" /> : <span />}
          <div className="reply-text">
            <span>{`${e.outcome === "ask" ? `${voice} said ` : ""}“${e.transcript || ""}”`}</span>
            {e.has_request_audio && (
              <span className="anchor">
                <button
                  className="chip"
                  aria-haspopup="menu"
                  aria-expanded={menuOpen(s, chip)}
                  aria-label={`Voice: ${voice}. Change`}
                  onClick={ev => toggleMenu(chip, ev.currentTarget)}
                >
                  {voice}
                  <Icon name="chev-down" size="s" />
                </button>
              </span>
            )}
          </div>
        </div>
      ) : e.outcome === "ask" ? (
        <div className="reason" style={{ marginTop: -6 }}>
          Nobody answered.
        </div>
      ) : null}
      <div className="answers" role="group" aria-label="Was it really hey TARS?">
        {answer("real", "hey TARS")}
        {answer("not_real", "Not it")}
      </div>
      {!reviewed && (
        <div className="reason">
          {`${e.auto_label ? GUESS[e.auto_label] : "No guess."} ${capital(e.auto_reason || "")}${e.auto_reason ? "." : ""} `}
          <span className="score">{score(e, s)}</span>
        </div>
      )}
    </article>
  );
}
