import { plural } from "../format";
import { type State, menuOpen, recluster, rename, toggleMenu, useStore, voiceName } from "../store";
import type { Cluster } from "../types";
import { Empty, PageHead, SecHead } from "./Blocks";
import { Icon } from "./Icon";
import { PlaySmall } from "./PlayButton";

export function Voices() {
  const s = useStore();
  const canRegroup = s.status.clustering;
  const head = (
    <>
      <PageHead title="Voices" sub="TARS groups requests by voice. Name a voice and TARS greets them by name.">
        {canRegroup && (
          <button className="btn big" disabled={s.reclustering} onClick={recluster}>
            <Icon name="refresh" size="s" />
            {s.reclustering ? "Regrouping…" : "Regroup voices"}
          </button>
        )}
      </PageHead>
      <p className="hint head-hint">
        {canRegroup
          ? "Regrouping sorts every request into voices again. Anything you've named, moved or merged stays that way."
          : "Regrouping voices isn't set up on this TARS."}
      </p>
      {s.reclustering && (
        <div className="running" role="status">
          <Icon name="wave" />
          <span>Regrouping voices. This can take a minute; the page stays as it is until it's done.</span>
        </div>
      )}
    </>
  );
  if (!s.clusters.length)
    return (
      <>
        {head}
        <Empty title="No voices yet" text="Once a few people have asked TARS something, their voices show up here." />
      </>
    );
  const ppl = s.clusters.filter(c => c.kind !== "not_person");
  const not = s.clusters.filter(c => c.kind === "not_person");
  return (
    <>
      {head}
      <SecHead title="People" />
      <div className="voices">
        {ppl.map(c => (
          <Voice key={c.id} c={c} s={s} />
        ))}
      </div>
      {not.length > 0 && (
        <>
          <SecHead title="Not people">
            <span className="muted">TV, radio and the like. TARS ignores them.</span>
          </SecHead>
          <div className="voices">
            {not.map(c => (
              <Voice key={c.id} c={c} s={s} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

function Voice({ c, s }: { c: Cluster; s: State }) {
  const name = voiceName(c);
  const unnamed = !c.name && c.kind === "unknown";
  const left = c.name && c.kind === "person" ? Math.max(0, s.status.enroll_at - c.size) : 0;
  const menu = { kind: "voice", id: c.id } as const;
  return (
    <article className="voice" data-id={c.id}>
      <div className="voice-head">
        <div>
          <h3 className="voice-name">{name}</h3>
          <div className="voice-meta">
            {`${plural(c.size, "request")}${left ? `. ${left} more before TARS knows this voice well` : ""}${unnamed ? ". Not named yet" : ""}.`}
          </div>
        </div>
        <div className="btns">
          {unnamed && (
            <button className="btn primary" onClick={() => rename(c.id)}>
              Name it
            </button>
          )}
          <div className="anchor">
            <button
              className="icon-btn"
              aria-haspopup="menu"
              aria-expanded={menuOpen(s, menu)}
              aria-label={`More for ${name}`}
              onClick={e => toggleMenu(menu, e.currentTarget)}
            >
              <Icon name="more" />
            </button>
          </div>
        </div>
      </div>
      {c.samples.length > 0 && (
        <div className="samples">
          {c.samples.map(x => (
            <div key={x.event_id} className="sample">
              <PlaySmall clip={{ event: x.event_id, part: "request" }} label={`a request from ${name}`} />
              <span>{`“${x.transcript || "…"}”`}</span>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}
