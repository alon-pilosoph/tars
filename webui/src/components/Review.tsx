import { KIND, isToday, kindOf, sentence, shortDay, stamp, time } from "../format";
import { type State, cName, menuOpen, reviewEvents, setLabel, toggleMenu, useStore } from "../store";
import type { Label, TarsEvent } from "../types";
import { PlayButton } from "./PlayButton";
import { Slabs } from "./Slabs";

export function Review() {
  const s = useStore();
  // Rows stay in their group until Refresh, even after they're answered (or an answer is cleared).
  const all = reviewEvents(s),
    todo = all.filter(e => s.snapshot.review.has(e.id)),
    done = all.filter(e => !s.snapshot.review.has(e.id));
  const live = todo.filter(e => !e.label).length;
  return (
    <>
      <div className="head">
        <ReviewHead live={live} shown={todo.length} />
      </div>
      {todo.length ? (
        <Group title="To check" events={todo} s={s} />
      ) : (
        <div className="empty">
          <Slabs />
          <h2>All clear.</h2>
          <p>TARS understood every wake since you last looked.</p>
          <div className="quip">confidence 97%, smugness 0%</div>
        </div>
      )}
      {done.length > 0 && <Group title="Reviewed" events={done} s={s} />}
    </>
  );
}

function ReviewHead({ live, shown }: { live: number; shown: number }) {
  if (live)
    return (
      <div>
        <h1>
          <span className="n">{live}</span> to check
        </h1>
        <p>TARS wasn't sure these were meant for it. Its guess is highlighted.</p>
      </div>
    );
  if (shown)
    return (
      <div>
        <h1>All checked</h1>
        <p>Refresh to move these to Reviewed.</p>
      </div>
    );
  return (
    <div>
      <h1>Nothing to check</h1>
      <p>When TARS isn't sure it heard its name, it shows up here.</p>
    </div>
  );
}

function Group({ title, events, s }: { title: string; events: TarsEvent[]; s: State }) {
  return (
    <section className="group">
      <h2 className="group-h">
        <b>{title}</b>
        {events.length}
      </h2>
      <div className="list">
        {events.map(e => (
          <Row key={e.id} e={e} s={s} />
        ))}
      </div>
    </section>
  );
}

function Row({ e, s }: { e: TarsEvent; s: State }) {
  const k = kindOf(e);
  return (
    <article className={`ev${e.label ? " done" : ""}`} aria-label={`${KIND[k]}, ${stamp(e.ts)}`}>
      <PlayButton clip={{ event: e.id, part: "wake" }} label="what woke it" />
      <div className="when">
        <span className="t" title={new Date(e.ts * 1000).toLocaleString()}>
          {time(e.ts)}
        </span>
        {!isToday(e.ts) && <span className="d">{shortDay(e.ts)}</span>}
        <span className={`kind k-${k}`}>{KIND[k]}</span>
      </div>
      <div className="heard">
        <Heard e={e} s={s} />
      </div>
      <div className="then">
        <Then e={e} s={s} />
      </div>
      <LabelControl e={e} />
      <button
        className="more"
        aria-haspopup="menu"
        aria-expanded={menuOpen(s, "event", e.id)}
        aria-label="More actions"
        onClick={ev => toggleMenu({ kind: "event", id: e.id }, ev.currentTarget)}
      />
    </article>
  );
}

function Heard({ e, s }: { e: TarsEvent; s: State }) {
  const score = e.wake_score?.toFixed(2) ?? "?";
  if (e.kind === "near_miss") {
    const th = s.models?.active?.threshold;
    return (
      <>
        <div className="cell-l">Heard</div>
        <div className="q none">Below the wake threshold</div>
        <div className="meta">
          <span>
            wake {score}
            {th != null ? ` / ${th}` : ""}
          </span>
        </div>
      </>
    );
  }
  const pct = e.confidence != null ? Math.round(e.confidence * 100) : null;
  return (
    <>
      <div className="cell-l">Heard</div>
      <div className="q">“{e.heard || "?"}”</div>
      <div className="meta">
        {pct != null && (
          <>
            <span className="bar" aria-hidden="true">
              <i style={{ width: `${pct}%` }} />
            </span>
            <span>{pct}% sure</span>
          </>
        )}
        <span>wake {score}</span>
      </div>
    </>
  );
}

function Then({ e, s }: { e: TarsEvent; s: State }) {
  const k = kindOf(e);
  const txt = (none: string, pre = false) => (
    <div className="txt">
      <div className="cell-l">Then</div>
      {pre && <Asked />}
      <div className="req none">{none}</div>
    </div>
  );
  if (k === "near_miss") return txt("Didn't wake");
  if (k === "ignore") return txt("Stayed quiet");
  if (e.transcript || e.utterance_audio)
    return (
      <>
        {e.utterance_audio && <PlayButton clip={{ event: e.id, part: "request" }} label="the request" sm />}
        <div className="txt">
          <div className="cell-l">Then</div>
          {k === "ask" && <Asked />}
          <div className={`req${e.transcript ? "" : " none"}`}>
            {e.transcript ? `“${e.transcript}”` : "No transcript"}
          </div>
          <Voice e={e} s={s} />
        </div>
      </>
    );
  return txt(nothingSaid(e.follow, k === "ask"), k === "ask");
}

function nothingSaid(follow: TarsEvent["follow"], asked: boolean) {
  if (follow === "said_nothing") return asked ? "Nobody answered" : "Nobody spoke";
  if (follow === "not_for_us") return "Not meant for TARS";
  return "Nothing followed";
}

const Asked = () => <div className="pre">Asked “Did you call me?”</div>;

function Voice({ e, s }: { e: TarsEvent; s: State }) {
  if (!e.utterance_audio) return null;
  const name = e.cluster_id != null ? cName(s, e.cluster_id) : null;
  return (
    <div className="who">
      <button
        className="chip"
        aria-haspopup="menu"
        aria-expanded={menuOpen(s, "eventVoice", e.id)}
        aria-label={`Voice: ${name || "none"}. Change`}
        onClick={ev => toggleMenu({ kind: "eventVoice", id: e.id }, ev.currentTarget)}
      >
        {name || "No voice"}
        {e.cluster_pinned ? <span className="pin">(pinned)</span> : null}
        <span className="car" />
      </button>
      {e.speaker && <span className="sid">speaker ID: {e.speaker}</span>}
    </div>
  );
}

const yn = (v: Label) => (v === "real" ? "yes" : "no");

function Reason({ e }: { e: TarsEvent }) {
  if (e.label)
    return (
      <>
        You said {yn(e.label)}.{e.auto_label && e.auto_label !== e.label ? ` TARS guessed ${yn(e.auto_label)}.` : ""}
      </>
    );
  const r =
    e.auto_reason && e.auto_reason !== "near-miss" ? e.auto_reason : e.kind === "near_miss" ? "it never woke up" : "";
  return (
    <>
      <b>{e.auto_label ? `TARS thinks ${yn(e.auto_label)}.` : "No guess."}</b>
      {r ? ` ${sentence(r)}` : ""}
    </>
  );
}

/** Was it really hey TARS? TARS's guess is highlighted until a person answers. */
function LabelControl({ e }: { e: TarsEvent }) {
  const guess = !e.label && e.auto_label;
  const b = (v: Label, cls: string, text: string) => (
    <button
      className={`${cls}${guess === v ? " guess" : ""}`}
      aria-pressed={e.label === v}
      onClick={() => setLabel(e.id, v)}
    >
      {text}
    </button>
  );
  return (
    <div className="lab">
      <div className="lab-seg" role="group" aria-label="Was it really hey TARS?">
        {b("real", "y", "hey TARS")}
        {b("not_real", "n", "Not it")}
      </div>
      <div className="reason">
        <Reason e={e} />
      </div>
    </div>
  );
}
