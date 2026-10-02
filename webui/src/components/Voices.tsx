import { plural } from "../format";
import {
  type MenuTarget,
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
import { Empty, Group } from "./Blocks";
import { PlayButton } from "./PlayButton";

export function Voices() {
  const s = useStore();
  const canRegroup = s.status.clustering;
  const head = (
    <div className="head">
      <div>
        <h1>Voices</h1>
        <p>The people TARS has heard. Name someone and TARS greets them by name.</p>
      </div>
      {canRegroup && (
        <button
          className="btn"
          disabled={s.reclustering}
          onClick={recluster}
          title="Anything you've named, moved or merged stays that way."
        >
          {s.reclustering ? (
            <>
              <span className="spin" />
              Regrouping…
            </>
          ) : (
            "Regroup voices"
          )}
        </button>
      )}
    </div>
  );
  if (!s.clusters.length)
    return (
      <>
        {head}
        <Empty
          slabs
          title="No voices yet."
          quip="nobody here but me"
          text={
            canRegroup
              ? "Once TARS has heard a few requests, press Regroup voices and it will sort them by who's speaking."
              : "Regrouping voices isn't set up on this assistant."
          }
        />
      </>
    );

  const section = (title: string, clusters: Cluster[]) =>
    clusters.length > 0 && (
      <Group title={title} count={clusters.length}>
        <div className={`voice-grid${s.reclustering ? " busy" : ""}`}>
          {clusters.map(c => (
            <VoiceCard key={c.id} c={c} s={s} />
          ))}
        </div>
      </Group>
    );
  return (
    <>
      {head}
      {s.reclustering && (
        <div className="progress" role="progressbar" aria-label="Regrouping">
          <i />
        </div>
      )}
      {section("People", people(s))}
      {section("Unnamed", unnamedVoices(s))}
      {section("Not people", notPeople(s))}
    </>
  );
}

function VoiceStatus({ c, enrollAt }: { c: Cluster; enrollAt: number }) {
  if (c.kind === "not_person") return <div className="voice-status">Not a person, so it's left out of voiceprints</div>;
  if (!c.name) return <div className="voice-status">Name it so TARS can recognize them</div>;
  if (c.size >= enrollAt) return <div className="voice-status ok">✓ Voiceprint from {c.size} requests</div>;
  return (
    <div className="voice-status">
      <span className="pips" aria-hidden="true">
        {Array.from({ length: enrollAt }, (_, i) => (
          <i key={i} className={i < c.size ? "filled" : ""} />
        ))}
      </span>
      {plural(enrollAt - c.size, "more request")} for a voiceprint
    </div>
  );
}

function VoiceCard({ c, s }: { c: Cluster; s: State }) {
  const name = voiceName(c);
  const menu: MenuTarget = { kind: "voice", id: c.id };
  const first = c.samples[0];
  return (
    <div className={`voice${c.kind === "not_person" ? " not-person" : ""}`}>
      <div className="voice-head">
        <div className="voice-name">
          <h3 className={c.name ? "" : "unnamed"}>{name}</h3>
          <div className="voice-count">{plural(c.size, "request")}</div>
        </div>
        <button
          className="more"
          aria-haspopup="menu"
          aria-expanded={menuOpen(s, menu)}
          aria-label={`More for ${name}`}
          onClick={ev => toggleMenu(menu, ev.currentTarget)}
        />
      </div>
      <div className="samples">
        {c.samples.map((sample, i) => (
          <PlayButton
            key={sample.event_id}
            clip={{ event: sample.event_id, part: "request" }}
            label={`sample ${i + 1} of ${name}`}
            small
            title={sample.transcript}
          />
        ))}
        {first?.transcript ? (
          <span className="sample-text">“{first.transcript}”</span>
        ) : first ? null : (
          <span className="sample-text">No samples</span>
        )}
      </div>
      <VoiceStatus c={c} enrollAt={s.status.enroll_at} />
      {!c.name && c.kind !== "not_person" && (
        <div className="voice-foot">
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
