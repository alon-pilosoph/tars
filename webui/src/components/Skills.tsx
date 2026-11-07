import { Fragment } from "react";
import { personName, stamp } from "../format";
import { deleteSkill, forgetPreference, pauseSkill, useStore } from "../store";
import type { Preference, Skill, SkillRun } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";

const SUB = "What TARS was taught by talking: things to do by name, some on their own, and what each of you likes.";
const TEACH = "To teach it something new, or change one, just tell TARS: “from now on, when I say bedtime…”.";

/** Whose: "Everyone's" for the household's, else the person's name. */
const whose = (owner: string) => (owner === "household" ? "Everyone's" : `${personName(owner)}'s`);

export function Skills() {
  const s = useStore();
  const info = s.skills;
  if (!info) return <PageHead title="Skills" sub={SUB} />; // they never loaded: LoadErrors says why
  const { skills, preferences, runs } = info;
  if (!skills.length && !preferences.length && !runs.length)
    return (
      <>
        <PageHead title="Skills" sub={SUB} />
        <Empty title="Nothing taught yet" text={TEACH} />
      </>
    );
  const owners = [...new Set(skills.map(sk => sk.owner))].sort((a, b) =>
    a === "household" ? -1 : b === "household" ? 1 : a.localeCompare(b),
  );
  const people = [...new Set(preferences.map(p => p.person))].sort();
  return (
    <>
      <PageHead title="Skills" sub={SUB} />
      <p className="r-meta skills-teach">{TEACH}</p>
      {owners.map(owner => (
        <Fragment key={owner}>
          <SecHead title={`${whose(owner)} skills`} />
          <div className="rem-rows">
            {skills
              .filter(sk => sk.owner === owner)
              .map(sk => (
                <SkillRow key={sk.id} sk={sk} />
              ))}
          </div>
        </Fragment>
      ))}
      {people.map(person => (
        <Fragment key={person}>
          <SecHead title={`What TARS knows about ${personName(person)}`} />
          <div className="rem-rows">
            {preferences
              .filter(p => p.person === person)
              .map(p => (
                <PreferenceRow key={p.id} p={p} />
              ))}
          </div>
        </Fragment>
      ))}
      {runs.length > 0 && (
        <>
          <SecHead title="Lately, on its own" />
          <div className="rem-rows">
            {runs.map(r => (
              <RunRow key={r.id} r={r} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

function SkillRow({ sk }: { sk: Skill }) {
  const when = [
    sk.when && `For when ${sk.when}.`,
    sk.runs_by_itself && `Runs by itself ${sk.runs_by_itself}, ${sk.say === "chat" ? "in the chat" : "said aloud"}.`,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <article className={`rem skill${sk.paused ? " past" : ""}`} data-id={sk.id}>
      <div className="rem-when skill-name">{sk.name}</div>
      <div>
        <p className="says">{sk.instructions}</p>
        <div className="r-meta">
          {sk.paused && <span className="skill-paused">Paused. </span>}
          {when || "When asked for by name."}
        </div>
      </div>
      <div className="btns">
        {sk.runs_by_itself && (
          <button className="btn" onClick={() => pauseSkill(sk, !sk.paused)}>
            {sk.paused ? "Resume" : "Pause"}
          </button>
        )}
        <button className="btn ghost" onClick={() => deleteSkill(sk)}>
          Delete
        </button>
      </div>
    </article>
  );
}

function PreferenceRow({ p }: { p: Preference }) {
  return (
    <article className="rem" data-id={p.id}>
      <div className="rem-when">{p.about}</div>
      <div>
        <p className="says">{p.text}</p>
        <div className="r-meta">{p.guessed ? `A guess TARS made, ${stamp(p.at)}.` : `Said ${stamp(p.at)}.`}</div>
      </div>
      <button className="btn ghost" onClick={() => forgetPreference(p)}>
        Forget
      </button>
    </article>
  );
}

function RunRow({ r }: { r: SkillRun }) {
  return (
    <article className="rem past" data-id={r.id}>
      <div className="rem-when">{stamp(r.at)}</div>
      <div>
        <p className="says">{r.said ? `“${r.said}”` : "Nothing to do."}</p>
        <div className="r-meta">{`${r.name}, ${r.why}.`}</div>
      </div>
      <span />
    </article>
  );
}
