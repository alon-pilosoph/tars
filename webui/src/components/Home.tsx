import { day, plural } from "../format";
import type { PersonFilter } from "../params";
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
import { Empty, Group } from "./Blocks";
import { ConversationCard } from "./Conversation";
import { ItemCard } from "./Items";

function PersonSeg({ s, shown, household }: { s: State; shown: PersonFilter; household: boolean }) {
  const options: { value: PersonFilter; label: string }[] = [
    { value: "all", label: "Everyone" },
    ...people(s).map(c => ({ value: c.id, label: c.name })),
    ...(household ? [{ value: "household" as const, label: "Household" }] : []),
  ];
  return (
    <div className="seg" role="group" aria-label="Show">
      {options.map(({ value, label }) => (
        <button key={value} aria-pressed={shown === value} onClick={() => setPerson(value)} title={label}>
          <span className="seg-label">{label}</span>
        </button>
      ))}
    </div>
  );
}

function ByDay({ convs, s }: { convs: Conversation[]; s: State }) {
  const days: { day: string; convs: Conversation[] }[] = [];
  for (const c of convs) {
    const d = day(c.started);
    if (days[days.length - 1]?.day !== d) days.push({ day: d, convs: [] });
    days[days.length - 1].convs.push(c);
  }
  return (
    <>
      {days.map(g => (
        <Group key={g.day} title={g.day} count={g.convs.length}>
          <div className="conv-list">
            {g.convs.map(c => (
              <ConversationCard key={c.id} c={c} s={s} />
            ))}
          </div>
        </Group>
      ))}
    </>
  );
}

function HomeHead({ unseenCount, newCount, name }: { unseenCount: number; newCount: number; name: string | null }) {
  if (unseenCount)
    return (
      <div>
        <h1>
          <span className="head-count">{unseenCount}</span> new from TARS
        </h1>
        <p>What TARS sent since you last looked.</p>
      </div>
    );
  if (newCount)
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
        <Empty
          slabs
          title="Nothing yet."
          quip="humor 75%, discretion 100%"
          text={
            "Say “hey TARS” and ask for something. The conversation shows up here, and so does anything TARS sends " +
            "you."
          }
        />
      </>
    );
  const who = personShown(s, false);
  const name = typeof who === "number" ? clusterName(s, who) : null;
  const newItems = s.items.filter(i => s.snapshot.newItems.has(i.id) && forPerson(i, who));
  const unseenCount = newItems.filter(i => !i.seen).length;
  const convs = s.convs.filter(c => convPerson(c, who));
  const toReview = reviewTodo(s).length;
  return (
    <>
      <div className="head">
        <HomeHead unseenCount={unseenCount} newCount={newItems.length} name={name} />
        <div className="head-side">
          {people(s).length > 1 ? <PersonSeg s={s} shown={who} household={false} /> : null}
        </div>
      </div>
      {newItems.length ? (
        <div className="item-grid">
          {newItems.map(i => (
            <ItemCard key={i.id} i={i} place="home" s={s} />
          ))}
        </div>
      ) : null}
      {toReview && who === "all" ? (
        <button className="nudge" onClick={() => setTab("review")}>
          <span className="count">{toReview}</span>
          <span>
            <b>{plural(toReview, "wake")} to check.</b> TARS wasn't sure {toReview === 1 ? "it was" : "they were"} meant
            for it.
          </span>
          <span className="nudge-arrow">Review →</span>
        </button>
      ) : null}
      {convs.length ? (
        <ByDay convs={convs} s={s} />
      ) : name ? (
        <Empty
          className="spaced"
          title={`No conversations with ${name} yet.`}
          text={`When TARS recognizes ${name}'s voice, their conversations show up here.`}
        >
          <button className="link" onClick={() => setPerson("all")}>
            Show everyone
          </button>
        </Empty>
      ) : (
        <Empty
          className="spaced"
          title="No conversations yet."
          text="Say “hey TARS” and ask for something. The conversation shows up here."
        />
      )}
    </>
  );
}

export function Sent() {
  const s = useStore();
  const who = personShown(s, true);
  const head = (
    <div className="head">
      <div>
        <h1>Sent</h1>
        <p>Everything TARS sent. Everyone at home sees the same list.</p>
      </div>
      <div className="head-side">{s.items.length ? <PersonSeg s={s} shown={who} household /> : null}</div>
    </div>
  );
  if (!s.items.length)
    return (
      <>
        {head}
        <Empty
          slabs
          title="Nothing sent yet."
          quip="it's very quiet in here"
          text="Ask for something TARS can't say out loud: “send me that recipe”, “make a packing list”."
        />
      </>
    );
  const list = s.items.filter(i => forPerson(i, who)).sort((a, b) => b.ts - a.ts);
  if (!list.length) {
    const house = who === "household";
    const name = house ? "the household" : clusterName(s, typeof who === "number" ? who : null);
    return (
      <>
        {head}
        <Empty
          title={`Nothing for ${name} yet.`}
          text={`Things TARS sends ${house ? "to the whole house" : `to ${name}`} show up here.`}
        >
          <button className="link" onClick={() => setPerson("all")}>
            Show everything
          </button>
        </Empty>
      </>
    );
  }
  // Opened items stay under New until Refresh; its count is how many are still unseen.
  const isNew = (i: Item) => s.snapshot.newItems.has(i.id);
  const fresh = list.filter(isNew);
  const earlier = list.filter(i => !isNew(i));
  const section = (title: string, items: Item[], count: number) =>
    items.length > 0 && (
      <Group title={title} count={count}>
        <div className="item-grid">
          {items.map(i => (
            <ItemCard key={i.id} i={i} place="sent" s={s} />
          ))}
        </div>
      </Group>
    );
  return (
    <>
      {head}
      {section("New", fresh, fresh.filter(i => !i.seen).length)}
      {section("Earlier", earlier, earlier.length)}
    </>
  );
}
