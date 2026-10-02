import { KIND_LABEL, heardText, isToday, kindOf, sentence, shortDay, stamp, time } from "../format";
import {
  type MenuTarget,
  type State,
  answerWake,
  clusterName,
  menuOpen,
  reviewEvents,
  toggleMenu,
  useStore,
} from "../store";
import type { Label, TarsEvent } from "../types";
import { Empty, Group } from "./Blocks";
import { PlayButton } from "./PlayButton";

export function Review() {
  const s = useStore();
  // Rows stay in their group until Refresh, even after an answer is given or cleared.
  const all = reviewEvents(s);
  const toCheck = all.filter(e => s.snapshot.review.has(e.id));
  const reviewed = all.filter(e => !s.snapshot.review.has(e.id));
  const unanswered = toCheck.filter(e => !e.label).length;
  return (
    <>
      <div className="head">
        <ReviewHead unanswered={unanswered} shown={toCheck.length} />
      </div>
      {toCheck.length ? (
        <Group title="To check" count={unanswered}>
          <WakeList events={toCheck} s={s} />
        </Group>
      ) : all.length ? (
        <Empty slabs title="All clear." text="Nothing new since you last looked." />
      ) : (
        <Empty slabs title="No wakes yet." text="TARS logs every wake. The ones it wasn't sure about come here." />
      )}
      {reviewed.length > 0 && (
        <Group title="Reviewed" count={reviewed.length}>
          <WakeList events={reviewed} s={s} />
        </Group>
      )}
    </>
  );
}

function ReviewHead({ unanswered, shown }: { unanswered: number; shown: number }) {
  if (unanswered)
    return (
      <div>
        <h1>
          <span className="head-count">{unanswered}</span> to check
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
    </div>
  );
}

function WakeList({ events, s }: { events: TarsEvent[]; s: State }) {
  return (
    <div className="wake-list">
      {events.map(e => (
        <WakeRow key={e.id} e={e} s={s} />
      ))}
    </div>
  );
}

function WakeRow({ e, s }: { e: TarsEvent; s: State }) {
  const kind = kindOf(e);
  const menu: MenuTarget = { kind: "event", id: e.id };
  return (
    <article className={`wake-row${e.label ? " answered" : ""}`} aria-label={`${KIND_LABEL[kind]}, ${stamp(e.ts)}`}>
      {e.has_wake_audio ? (
        <PlayButton clip={{ event: e.id, part: "wake" }} label="what woke it" />
      ) : (
        <span className="play-gap" />
      )}
      <div className="when">
        <span className="when-time" title={stamp(e.ts)}>
          {time(e.ts)}
        </span>
        {!isToday(e.ts) && <span className="when-day">{shortDay(e.ts)}</span>}
        <span className={`kind k-${kind}`}>{KIND_LABEL[kind]}</span>
      </div>
      <div className="heard">
        <Heard e={e} s={s} />
      </div>
      <div className="then">
        <Then e={e} s={s} />
      </div>
      <AnswerControl e={e} />
      <button
        className="more"
        aria-haspopup="menu"
        aria-expanded={menuOpen(s, menu)}
        aria-label={`More for the wake at ${time(e.ts)}`}
        onClick={ev => toggleMenu(menu, ev.currentTarget)}
      />
    </article>
  );
}

function Heard({ e, s }: { e: TarsEvent; s: State }) {
  const score = e.wake_score?.toFixed(2) ?? "—";
  if (e.kind === "near_miss") {
    const threshold = s.models?.active?.threshold;
    return (
      <>
        <div className="cell-label">Heard</div>
        <div className="heard-text none">Below the wake threshold</div>
        <div className="meta">
          <span>
            wake {score}
            {threshold != null ? ` / ${threshold}` : ""}
          </span>
        </div>
      </>
    );
  }
  const pct = e.confidence != null ? Math.round(e.confidence * 100) : null;
  return (
    <>
      <div className="cell-label">Heard</div>
      <div className="heard-text">“{e.heard ? heardText(e.heard) : "—"}”</div>
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
  const kind = kindOf(e);
  const nothing = (text: string, asked = false) => (
    <>
      <div className="cell-label">Then</div>
      <div className="then-text">
        {asked && <Asked />}
        <div className="request none">{text}</div>
      </div>
    </>
  );
  if (kind === "near_miss") return nothing("Didn't wake");
  if (kind === "ignore") return nothing("Stayed quiet");
  if (e.transcript || e.has_request_audio)
    return (
      <>
        <div className="cell-label">Then</div>
        <div className="then-body">
          {e.has_request_audio && <PlayButton clip={{ event: e.id, part: "request" }} label="the request" small />}
          <div className="then-text">
            {kind === "ask" && <Asked />}
            <div className={`request${e.transcript ? "" : " none"}`}>
              {e.transcript ? `“${e.transcript}”` : "No transcript"}
            </div>
            <Voice e={e} s={s} />
          </div>
        </div>
      </>
    );
  return nothing(nothingSaid(e.follow, kind === "ask"), kind === "ask");
}

function nothingSaid(follow: TarsEvent["follow"], asked: boolean) {
  if (follow === "said_nothing") return asked ? "Nobody answered" : "Nobody spoke";
  if (follow === "not_for_us") return "Not meant for TARS";
  return "Nothing followed";
}

const Asked = () => <div className="asked">Asked “Did you call me?”</div>;

function Voice({ e, s }: { e: TarsEvent; s: State }) {
  if (!e.has_request_audio) return null;
  const name = clusterName(s, e.cluster_id);
  const menu: MenuTarget = { kind: "eventVoice", id: e.id };
  return (
    <div className="voice-line">
      <button
        className="chip"
        aria-haspopup="menu"
        aria-expanded={menuOpen(s, menu)}
        aria-label={`Voice: ${name || "none"}. Change`}
        onClick={ev => toggleMenu(menu, ev.currentTarget)}
      >
        <span className="chip-name" title={name || undefined}>
          {name || "No voice"}
        </span>
        {e.cluster_pinned ? <span className="chip-pin">(pinned)</span> : null}
        <span className="chip-caret" />
      </button>
    </div>
  );
}

const yesNo = (v: Label) => (v === "real" ? "yes" : "no");

function Reason({ e }: { e: TarsEvent }) {
  if (e.label) {
    const guessed = e.auto_label && e.auto_label !== e.label ? ` TARS guessed ${yesNo(e.auto_label)}.` : "";
    return (
      <>
        You said {yesNo(e.label)}.{guessed}
      </>
    );
  }
  return (
    <>
      <b>{e.auto_label ? `TARS thinks ${yesNo(e.auto_label)}.` : "No guess."}</b>
      {e.auto_reason ? ` ${sentence(e.auto_reason)}` : ""}
    </>
  );
}

function AnswerControl({ e }: { e: TarsEvent }) {
  const guess = !e.label && e.auto_label;
  const option = (value: Label, className: string, text: string) => (
    <button
      className={`${className}${guess === value ? " guess" : ""}`}
      aria-pressed={e.label === value}
      onClick={() => answerWake(e.id, value)}
    >
      {text}
    </button>
  );
  return (
    <div className="answer">
      <div className="answer-seg" role="group" aria-label="Was it really hey TARS?">
        {option("real", "answer-yes", "hey TARS")}
        {option("not_real", "answer-no", "Not it")}
      </div>
      <div className="reason">
        <Reason e={e} />
      </div>
    </div>
  );
}
