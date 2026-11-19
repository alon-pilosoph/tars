/* The Voices page: the people TARS knows by voice, never anonymous voices. What it asks is one request at a time
   ("Who said this?", with TARS's guess), and, under each person, the few of theirs that sound unlike the rest. Every
   answer is final (people.py). */
import { useEffect, useState } from "react";
import { plural, roomName, stamp } from "../format";
import { answerWho, loadPeople, someoneNew, useStore } from "../store";
import type { Heard, PeoplePage, Person } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";
import { PlaySmall } from "./PlayButton";

const SPEAKERPHONE = "home"; // the room of TARS's own speakerphone (rooms.MAIN)

const mic = (room: string) =>
  room === SPEAKERPHONE ? "On the speakerphone" : `In the ${roomName(room).toLowerCase()}`;

export function Voices() {
  const s = useStore();
  useEffect(() => {
    loadPeople();
  }, []);
  const head = (
    <PageHead
      title="Voices"
      sub="The people TARS knows by voice: it greets them by name, and knows whose each request is."
    />
  );
  const page = s.people;
  if (!page)
    return (
      <>
        {head}
        {s.peopleError ? (
          <Empty title="Couldn't load the voices" text={s.peopleError} />
        ) : (
          <p className="hint">Loading…</p>
        )}
      </>
    );
  return (
    <>
      {head}
      {page.ask.length > 0 && <WhoSaid page={page} />}
      <SecHead title="People" />
      <div className="voices">
        {page.people.map(p => (
          <PersonCard key={p.id} p={p} enrollAt={page.enroll_at} />
        ))}
      </div>
      <button className="btn ghost big add-person" onClick={() => someoneNew()}>
        Add someone
      </button>
    </>
  );
}

function WhoSaid({ page }: { page: PeoplePage }) {
  const [card, ...rest] = page.ask;
  const id = card.event_id;
  return (
    <section className="who-said" aria-label="Who said this?">
      <h2 className="sec-title">Who said this?</h2>
      <div className="sample">
        <PlaySmall clip={{ event: id, part: "request" }} label="the request" />
        <span>{`“${card.transcript || "…"}”`}</span>
      </div>
      <div className="who-where">{mic(card.room)}</div>
      <div className="who-where">{stamp(card.ts)}</div>
      {card.guess && <p className="hint who-guess">{`Sounds like ${card.guess.name}.`}</p>}
      <div className="who-btns">
        {page.people.map(p => (
          <button
            key={p.id}
            className={`btn big ${card.guess?.id === p.id ? "primary" : ""}`}
            onClick={() => answerWho(id, p.id, `Saved as ${p.name}'s.`)}
          >
            {p.name}
          </button>
        ))}
        <button className="btn big" onClick={() => someoneNew(id)}>
          Someone else
        </button>
        <button className="btn big" onClick={() => answerWho(id, "tv", "Saved as the TV or radio.")}>
          TV or radio
        </button>
        <button className="btn ghost big" onClick={() => answerWho(id, "unsure", "Left out: TARS won't ask again.")}>
          Not sure
        </button>
      </div>
      {rest.length > 0 && <p className="hint">{`${rest.length} more after this one.`}</p>}
    </section>
  );
}

function PersonCard({ p, enrollAt }: { p: Person; enrollAt: number }) {
  const [teach, setTeach] = useState(false);
  const [all, setAll] = useState(false);
  return (
    <article className="voice person" data-id={p.id}>
      <h3 className="voice-name">{p.name}</h3>
      {p.mics.length === 0 ? (
        <div className="voice-meta">{`TARS doesn't know ${p.name}'s voice yet.`}</div>
      ) : (
        p.mics.map(m => (
          <div key={m.room} className="voice-meta">
            {`${mic(m.room)}: ${
              m.requests >= enrollAt
                ? `knows ${p.name}'s voice (${plural(m.requests, "request")})`
                : `${m.requests} of ${enrollAt} requests before TARS knows ${p.name}'s voice there`
            }`}
          </div>
        ))
      )}
      {p.check.length > 0 && (
        <div className="person-check">
          <p className="hint">{`These don't sound like the rest of ${p.name}'s. Are they ${p.name}'s?`}</p>
          {p.check.map(h => (
            <div key={h.event_id} className="sample">
              <PlaySmall clip={{ event: h.event_id, part: "request" }} label="the request" />
              <span>{`“${h.transcript || "…"}”`}</span>
              <button className="btn" onClick={() => answerWho(h.event_id, p.id, `Kept as ${p.name}'s.`)}>
                Yes
              </button>
              <button className="btn ghost" onClick={() => answerWho(h.event_id, "unsure", `No longer ${p.name}'s.`)}>
                No
              </button>
            </div>
          ))}
        </div>
      )}
      <button className="voices-fold" onClick={() => setTeach(!teach)}>
        {`How to teach TARS ${p.name}'s voice`}
        <Icon name={teach ? "chev-up" : "chev-down"} size="s" />
      </button>
      {teach && <Teach name={p.name} enrollAt={enrollAt} />}
      {p.shown.length > 0 && (
        <>
          <button className="voices-fold" onClick={() => setAll(!all)}>
            {all ? `Hide ${p.name}'s requests` : `${p.name}'s requests (${p.requests})`}
            <Icon name={all ? "chev-up" : "chev-down"} size="s" />
          </button>
          {all && (
            <div className="samples">
              {p.shown.map(h => (
                <Theirs key={h.event_id} h={h} name={p.name} />
              ))}
            </div>
          )}
        </>
      )}
    </article>
  );
}

function Theirs({ h, name }: { h: Heard; name: string }) {
  return (
    <div className="sample">
      <PlaySmall clip={{ event: h.event_id, part: "request" }} label="the request" />
      <span>{`“${h.transcript || "…"}”`}</span>
      <button className="btn ghost" onClick={() => answerWho(h.event_id, "unsure", `No longer ${name}'s.`)}>
        {`Not ${name}`}
      </button>
    </div>
  );
}

function Teach({ name, enrollAt }: { name: string; enrollAt: number }) {
  return (
    <div className="teach">
      <p>{`Alone with TARS in a room, close to it, in a normal voice, ${name} says a dozen things, each starting with
        "hey TARS", and waits for each answer. Like:`}</p>
      <ul>
        <li>What's the time?</li>
        <li>What's the weather going to be like tomorrow?</li>
        <li>Tell me something interesting about octopuses.</li>
        <li>Set a timer for one minute, then: cancel the timer.</li>
        <li>How many grams are in a cup of flour?</li>
      </ul>
      <p>{`Each then comes up here, under "Who said this?". Tap ${name}. After ${enrollAt} in a room, TARS knows ${name}'s voice there.`}</p>
    </div>
  );
}
