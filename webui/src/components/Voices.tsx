import { ENROLL_AT, plural } from "../format";
import {
  type State,
  menuOpen,
  notPeople,
  people,
  recluster,
  rename,
  toggleMenu,
  toggleNotPerson,
  unnamedVoices,
  useStore,
  voiceName,
} from "../store";
import type { Cluster } from "../types";
import { PlayButton } from "./PlayButton";
import { Slabs } from "./Slabs";

export function Voices() {
  const s = useStore();
  const can = s.status?.clustering !== false;
  const head = (
    <div className="head">
      <div>
        <h1>Voices</h1>
        <p>The people TARS has heard. Name someone and TARS greets them by name.</p>
      </div>
      {can && (
        <button
          className="btn"
          disabled={s.reclustering}
          onClick={recluster}
          title="Anything you've named, moved or merged stays that way."
        >
          {s.reclustering ? (
            <>
              <span className="spin" />
              Re-clustering…
            </>
          ) : (
            "Re-cluster voices"
          )}
        </button>
      )}
    </div>
  );
  if (!s.clusters.length)
    return (
      <>
        {head}
        <div className="empty">
          <Slabs />
          <h2>No voices yet.</h2>
          <p>
            {can
              ? "Once TARS has heard a few requests, press Re-cluster and it will sort them by who's speaking."
              : "Clustering isn't set up on this assistant."}
          </p>
          <div className="quip">nobody here but me</div>
        </div>
      </>
    );

  const section = (title: string, cs: Cluster[]) =>
    cs.length > 0 && (
      <section className="group">
        <h2 className="group-h">
          <b>{title}</b>
          {cs.length}
        </h2>
        <div className={`vgrid${s.reclustering ? " busy" : ""}`}>
          {cs.map(c => (
            <VoiceCard key={c.id} c={c} s={s} />
          ))}
        </div>
      </section>
    );
  return (
    <>
      {head}
      {s.reclustering && (
        <div className="progress" role="progressbar" aria-label="Re-clustering">
          <i />
        </div>
      )}
      {section("People", people(s))}
      {section("Unnamed", unnamedVoices(s))}
      {section("Not people", notPeople(s))}
    </>
  );
}

function VoiceCard({ c, s }: { c: Cluster; s: State }) {
  const nm = voiceName(c);
  const samples = s.events.filter(e => e.cluster_id === c.id && e.utterance_audio).slice(0, 4);
  let status;
  if (c.kind === "not_person") status = <div className="vs">Not a person, so it's left out of voiceprints</div>;
  else if (!c.name) status = <div className="vs">Name it so TARS can recognize them</div>;
  else if (c.size >= ENROLL_AT) status = <div className="vs ok">✓ Voiceprint from {c.size} requests</div>;
  else
    status = (
      <div className="vs">
        <span className="pips" aria-hidden="true">
          {Array.from({ length: ENROLL_AT }, (_, i) => (
            <i key={i} className={i < c.size ? "f" : ""} />
          ))}
        </span>
        {plural(ENROLL_AT - c.size, "more request")} for a voiceprint
      </div>
    );
  return (
    <div className={`voice${c.kind === "not_person" ? " np" : ""}`}>
      <div className="vh">
        <div className="nm">
          <h3 className={c.name ? "" : "un"}>{nm}</h3>
          <div className="c">{plural(c.size, "request")}</div>
        </div>
        <button
          className="more"
          aria-haspopup="menu"
          aria-expanded={menuOpen(s, "voiceCard", c.id)}
          aria-label={`More for ${nm}`}
          onClick={ev => toggleMenu({ kind: "voiceCard", id: c.id }, ev.currentTarget)}
        />
      </div>
      <div className="samples">
        {samples.map((e, i) => (
          <PlayButton
            key={e.id}
            clip={{ event: e.id, part: "request" }}
            label={`sample ${i + 1} of ${nm}`}
            sm
            title={e.transcript}
          />
        ))}
        {samples[0]?.transcript ? (
          <span className="sm-t">“{samples[0].transcript}”</span>
        ) : samples.length ? null : (
          <span className="sm-t">No samples</span>
        )}
      </div>
      {status}
      {!c.name && c.kind !== "not_person" && (
        <div className="vfoot">
          <button className="btn sm" onClick={() => rename(c.id)}>
            Name…
          </button>
          <button className="btn sm" onClick={() => toggleNotPerson(c.id)}>
            Not a person
          </button>
        </div>
      )}
    </div>
  );
}
