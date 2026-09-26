import { day } from "../format";
import type { PersonFilter } from "../params";
import {
  type State,
  cName,
  convPerson,
  forPerson,
  people,
  personShown,
  reviewTodo,
  setPerson,
  setTab,
  useStore,
} from "../store";
import type { Conversation as Conv } from "../types";
import { Conversation } from "./Conversation";
import { ItemCard } from "./Items";
import { Slabs } from "./Slabs";

function PersonSeg({ s, shown, household }: { s: State; shown: PersonFilter; household: boolean }) {
  const opts: [PersonFilter, string][] = [
    ["all", "Everyone"],
    ...people(s).map(c => [c.id, c.name] as [number, string]),
    ...(household ? [["household", "Household"] as [PersonFilter, string]] : []),
  ];
  return (
    <div className="seg" role="group" aria-label="Show">
      {opts.map(([k, l]) => (
        <button key={k} aria-pressed={shown === k} onClick={() => setPerson(k)}>
          {l}
        </button>
      ))}
    </div>
  );
}

function ByDay({ convs, s }: { convs: Conv[]; s: State }) {
  const days: [string, Conv[]][] = [];
  for (const c of convs) {
    const d = day(c.started);
    if (!days.length || days[days.length - 1][0] !== d) days.push([d, []]);
    days[days.length - 1][1].push(c);
  }
  return (
    <>
      {days.map(([d, cs]) => (
        <section className="group" key={d}>
          <h2 className="group-h">
            <b>{d}</b>
            {cs.length}
          </h2>
          <div className="convs">
            {cs.map(c => (
              <Conversation key={c.id} c={c} s={s} />
            ))}
          </div>
        </section>
      ))}
    </>
  );
}

function HomeHead({ live, news, name }: { live: number; news: number; name: string | null }) {
  if (live)
    return (
      <div>
        <h1>
          <span className="n">{live}</span> new from TARS
        </h1>
        <p>What TARS sent since you last looked.</p>
      </div>
    );
  if (news)
    return (
      <div>
        <h1>Nothing new</h1>
        <p>You've seen everything TARS sent. Refresh to clear this.</p>
      </div>
    );
  return (
    <div>
      <h1>Conversations</h1>
      <p>{name ? `What ${name} asked TARS, and what it said back.` : "What you asked TARS, and what it said back."}</p>
    </div>
  );
}

export function Home() {
  const s = useStore();
  if (!s.convs.length && !s.items.length)
    return (
      <>
        <div className="head">
          <div>
            <h1>Conversations</h1>
            <p>What you asked TARS, and what it said back.</p>
          </div>
        </div>
        <div className="empty">
          <Slabs />
          <h2>Nothing yet.</h2>
          <p>
            Say “hey TARS” and ask for something. The conversation shows up here, and so does anything TARS sends you.
          </p>
          <div className="quip">humor 75%, discretion 100%</div>
        </div>
      </>
    );
  const who = personShown(s, false),
    name = typeof who === "number" ? cName(s, who) : null;
  const news = s.items.filter(i => s.snapshot.newItems.has(i.id) && forPerson(i, who)),
    live = news.filter(i => !i.seen).length;
  const convs = s.convs.filter(c => convPerson(c, who)),
    r = reviewTodo(s).length;
  return (
    <>
      <div className="head">
        <HomeHead live={live} news={news.length} name={name} />
        <div className="aside-r">{people(s).length > 1 ? <PersonSeg s={s} shown={who} household={false} /> : null}</div>
      </div>
      {news.length ? (
        <div className="igrid">
          {news.map(i => (
            <ItemCard key={i.id} i={i} where="home" s={s} />
          ))}
        </div>
      ) : null}
      {r && who === "all" ? (
        <button className="nudge" onClick={() => setTab("review")}>
          <span className="count">{r}</span>
          <span>
            <b>{r === 1 ? "One wake" : `${r} wakes`} to check.</b> TARS wasn't sure {r === 1 ? "it was" : "they were"}{" "}
            meant for it.
          </span>
          <span className="arr">Review →</span>
        </button>
      ) : null}
      {convs.length ? (
        <ByDay convs={convs} s={s} />
      ) : (
        <div className="empty" style={{ marginTop: "var(--s5)" }}>
          <h2>No conversations with {name} yet.</h2>
          <p>When TARS recognizes {name}'s voice, their conversations show up here.</p>
          <button className="link" onClick={() => setPerson("all")}>
            Show everyone
          </button>
        </div>
      )}
    </>
  );
}

export function Sent() {
  const s = useStore(),
    who = personShown(s, true);
  const head = (
    <div className="head">
      <div>
        <h1>Sent</h1>
        <p>Everything TARS sent. Everyone at home sees the same list.</p>
      </div>
      <div className="aside-r">{s.items.length ? <PersonSeg s={s} shown={who} household /> : null}</div>
    </div>
  );
  if (!s.items.length)
    return (
      <>
        {head}
        <div className="empty">
          <Slabs />
          <h2>Nothing sent yet.</h2>
          <p>Ask for something TARS can't say out loud: “send me that recipe”, “make a packing list”.</p>
          <div className="quip">it's very quiet in here</div>
        </div>
      </>
    );
  const list = s.items.filter(i => forPerson(i, who)).sort((a, b) => b.ts - a.ts);
  if (!list.length) {
    const house = who === "household",
      name = house ? "the household" : cName(s, who as number);
    return (
      <>
        {head}
        <div className="empty">
          <h2>Nothing for {name} yet.</h2>
          <p>Things TARS sends {house ? "to the whole house" : `to ${name}`} show up here.</p>
          <button className="link" onClick={() => setPerson("all")}>
            Show everything
          </button>
        </div>
      </>
    );
  }
  const section = (title: string, items: typeof list) =>
    items.length > 0 && (
      <section className="group">
        <h2 className="group-h">
          <b>{title}</b>
          {items.length}
        </h2>
        <div className="igrid">
          {items.map(i => (
            <ItemCard key={i.id} i={i} where="sent" s={s} />
          ))}
        </div>
      </section>
    );
  const isNew = (i: (typeof list)[number]) => s.snapshot.newItems.has(i.id);
  return (
    <>
      {head}
      {section("New", list.filter(isNew))}
      {section(
        "Earlier",
        list.filter(i => !isNew(i)),
      )}
    </>
  );
}
