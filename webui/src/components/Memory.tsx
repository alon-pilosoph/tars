import { Fragment } from "react";
import { day, isRecent, longDate, personName, stamp } from "../format";
import { linkTo } from "../params";
import { allowAgain, editMemory, forgetMemory, neverLearn, useStore } from "../store";
import type { Memory as Known, NeverLearn } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";

const SUB = "What TARS knows about each of you and the household, and why. Private ones aren't shown here.";
const KINDS = [
  ["style", "How to answer"],
  ["like", "Likes"],
  ["fact", "Facts"],
  ["person", "People"],
  ["thread", "Coming up"],
] as const;

const whose = (person: string) => (person ? personName(person) : "The household");

export function Memory() {
  const info = useStore().memories;
  if (!info) return <PageHead title="Memory" sub={SUB} />; // they never loaded: LoadErrors says why
  const { memories, never } = info;
  if (!memories.length && !never.length)
    return (
      <>
        <PageHead title="Memory" sub={SUB} />
        <Empty
          title="Nothing known yet"
          text="TARS learns as you talk to it. You can also say “remember that the bins go out on Tuesday”."
        />
      </>
    );
  const people = [...new Set(memories.map(m => m.person))].sort((a, b) => (a && b ? a.localeCompare(b) : a ? -1 : 1));
  return (
    <>
      <PageHead title="Memory" sub={SUB} />
      {people.map(person => (
        <Fragment key={person}>
          <SecHead title={whose(person)} />
          {KINDS.map(([kind, label]) => {
            const these = memories.filter(m => m.person === person && m.kind === kind);
            return (
              these.length > 0 && (
                <Fragment key={kind}>
                  <h3 className="mem-kind">{label}</h3>
                  <div className="rem-rows">
                    {these.map(m => (
                      <MemoryRow key={m.id} m={m} />
                    ))}
                  </div>
                </Fragment>
              )
            );
          })}
        </Fragment>
      ))}
      {never.length > 0 && (
        <>
          <SecHead title="Never learn" />
          <div className="rem-rows">
            {never.map(n => (
              <NeverRow key={n.id} n={n} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

/** A memory's day, with the year when it isn't this one: some are kept for years. */
function dayOf(ts: number) {
  const year = new Date(ts * 1000).getFullYear();
  if (isRecent(ts)) return day(ts).toLowerCase();
  return year === new Date().getFullYear() ? longDate(ts) : `${longDate(ts)} ${year}`;
}
const onDay = (ts: number) => (isRecent(ts) ? dayOf(ts) : `on ${dayOf(ts)}`);

const SOURCES: Record<string, string> = { voice: " aloud", chat: " in a chat", web: " on a TARS page" };

const why = (m: Known) =>
  m.guessed ? `A guess TARS made ${onDay(m.created)}.` : `Said${SOURCES[m.source] ?? ""} ${onDay(m.created)}.`;

function MemoryRow({ m }: { m: Known }) {
  return (
    <article className="rem skill" data-id={m.id}>
      <div className="rem-when">{m.about}</div>
      <div>
        <p className="says">{m.text}</p>
        {m.due !== null && <div className="r-meta">{`Happens ${stamp(m.due)}.`}</div>}
        <div className="r-meta">{why(m)}</div>
        {m.heard && (
          <div className="r-meta">
            {`${m.heard_from ? personName(m.heard_from) : "Someone"} said “${m.heard}” `}
            {m.conversation !== null && (
              <a href={linkTo({ tab: "home", conv: m.conversation })}>See the conversation</a>
            )}
          </div>
        )}
        {m.history.map((h, i) => (
          <div className="r-meta" key={i}>{`Until ${dayOf(h.replaced)}: ${h.text}`}</div>
        ))}
      </div>
      <div className="btns">
        <button className="btn" onClick={() => editMemory(m)}>
          Edit
        </button>
        <button className="btn ghost" onClick={() => forgetMemory(m)}>
          Forget
        </button>
        <button className="btn ghost" onClick={() => neverLearn(m)}>
          Never learn
        </button>
      </div>
    </article>
  );
}

function NeverRow({ n }: { n: NeverLearn }) {
  return (
    <article className="rem skill past" data-id={n.id}>
      <div className="rem-when">{whose(n.person)}</div>
      <div>
        <p className="says">{n.text}</p>
        <div className="r-meta">{`Since ${dayOf(n.at)}.`}</div>
      </div>
      <div className="btns">
        <button className="btn ghost" onClick={() => allowAgain(n)}>
          Allow again
        </button>
      </div>
    </article>
  );
}
