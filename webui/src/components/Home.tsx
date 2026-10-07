import { Fragment } from "react";
import { day, isRecent, longDate } from "../format";
import { type PersonFilter, linkTo } from "../params";
import {
  type State,
  clusterName,
  convPerson,
  forPerson,
  people,
  personShown,
  reviewTodo,
  setPerson,
  setTab,
  useStore,
} from "../store";
import type { Conversation, Item } from "../types";
import { Empty } from "./Blocks";
import { ConversationRow } from "./Conversation";
import { NewRow, SentItem } from "./Items";

export function PersonSeg({ s, shown, household }: { s: State; shown: PersonFilter; household: boolean }) {
  const named = people(s);
  if (named.length < 2 && !household) return null;
  const options: { value: PersonFilter; label: string }[] = [
    { value: "all", label: "Everyone" },
    ...named.map(c => ({ value: c.id, label: c.name })),
    ...(household ? [{ value: "household" as const, label: "Household" }] : []),
  ];
  return (
    <div className="seg" role="group" aria-label="Whose">
      {options.map(({ value, label }) => (
        <button key={value} aria-pressed={shown === value} onClick={() => setPerson(value)}>
          {label}
        </button>
      ))}
    </div>
  );
}

function byDay(convs: Conversation[]) {
  const days: { day: string; ts: number; convs: Conversation[] }[] = [];
  for (const c of convs) {
    const d = day(c.started);
    if (days[days.length - 1]?.day !== d) days.push({ day: d, ts: c.started, convs: [] });
    days[days.length - 1].convs.push(c);
  }
  return days;
}

export function Home() {
  const s = useStore();
  if (!s.convs.length && !s.items.length)
    return (
      <Empty
        title="Nothing yet"
        text="TARS is plugged in, listening, and so far nobody has asked it anything. Say “hey TARS” and ask for something. It will show up here after a Refresh."
      />
    );
  const who = personShown(s, false);
  const name = typeof who === "number" ? clusterName(s, who) : null;
  const anyNew = s.items.some(i => s.snapshot.newItems.has(i.id));
  const newItems = s.items.filter(i => s.snapshot.newItems.has(i.id) && forPerson(i, who));
  const convs = s.convs.filter(c => convPerson(c, who));
  const toReview = reviewTodo(s).length;
  const seg = <PersonSeg s={s} shown={who} household={false} />;
  return (
    <>
      {anyNew && (
        <>
          <div className="sec-head">
            <h2 className="sec-title">New from TARS</h2>
            {seg}
          </div>
          {newItems.length ? (
            <div className="rows">
              {newItems.map(i => (
                <NewRow key={i.id} i={i} s={s} />
              ))}
            </div>
          ) : (
            <p className="muted">{`Nothing new for ${name}.`}</p>
          )}
        </>
      )}
      {toReview > 0 && (
        <div className="nudge">
          {`${toReview} wake${toReview === 1 ? "" : "s"} TARS wasn't sure about. `}
          <a
            href={linkTo({ tab: "review" })}
            onClick={e => {
              if (e.metaKey || e.ctrlKey || e.shiftKey) return;
              e.preventDefault();
              setTab("review");
            }}
          >
            {toReview === 1 ? "Review it" : "Review them"}
          </a>
        </div>
      )}
      {convs.length ? (
        byDay(convs).map((g, n) => (
          <section key={g.day}>
            <div className="day-head">
              <div>
                <h2>{g.day}</h2>
                {isRecent(g.ts) && <span className="date">{longDate(g.ts)}</span>}
              </div>
              {n === 0 && !anyNew && seg}
            </div>
            <div className="convs">
              {g.convs.map(c => (
                <ConversationRow key={c.id} c={c} s={s} />
              ))}
            </div>
          </section>
        ))
      ) : (
        <Empty
          title={name ? `No conversations with ${name}` : "No conversations yet"}
          text={
            name
              ? `When TARS recognises ${name}'s voice, their conversations show up here.`
              : "Conversations show up here after a Refresh."
          }
        >
          {name && (
            <div className="btns">
              <button className="btn" onClick={() => setPerson("all")}>
                Show everyone
              </button>
            </div>
          )}
        </Empty>
      )}
    </>
  );
}

export function Sent() {
  const s = useStore();
  const who = personShown(s, true);
  const head = (
    <div className="page-head">
      <div>
        <h1 className="page-title">Sent</h1>
        <p className="page-sub">Everything TARS sent. Everyone at home sees the same list.</p>
      </div>
      {s.items.length > 0 && <PersonSeg s={s} shown={who} household />}
    </div>
  );
  if (!s.items.length)
    return (
      <>
        {head}
        <Empty title="Nothing sent yet" text="Ask TARS to send you a recipe, a link or a list, and it lands here." />
      </>
    );
  const list = s.items.filter(i => forPerson(i, who)).sort((a, b) => b.ts - a.ts);
  if (!list.length) {
    const target = who === "household" ? "the household" : clusterName(s, typeof who === "number" ? who : null);
    return (
      <>
        {head}
        <Empty title={`Nothing for ${target}`} text="Try Everyone to see the rest.">
          <div className="btns">
            <button className="btn" onClick={() => setPerson("all")}>
              Show everything
            </button>
          </div>
        </Empty>
      </>
    );
  }
  // What was new at the last Refresh stays under New until the next one, even once it's been opened.
  const isNew = (i: Item) => s.snapshot.newItems.has(i.id);
  const fresh = list.filter(isNew);
  const earlier = list.filter(i => !isNew(i));
  const section = (title: string, items: Item[]) =>
    items.length > 0 && (
      <Fragment key={title}>
        <div className="sec-head">
          <h2 className="sec-title">{title}</h2>
        </div>
        <div className="items">
          {items.map(i => (
            <SentItem key={i.id} i={i} s={s} />
          ))}
        </div>
      </Fragment>
    );
  return (
    <>
      {head}
      {section("New", fresh)}
      {section(fresh.length ? "Earlier" : "Everything", earlier)}
    </>
  );
}
