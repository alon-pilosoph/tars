import { useState } from "react";
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
  const named = s.clusters.filter(c => c.kind === "person");
  const unnamed = s.clusters.filter(c => c.kind === "unknown");
  const groups = unnamed.filter(c => c.size >= GROUP_AT);
  const strays = unnamed.filter(c => c.size < GROUP_AT);
  const not = s.clusters.filter(c => c.kind === "not_person");
  return (
    <>
      {head}
      {named.length > 0 && (
        <>
          <SecHead title="People" />
          <div className="voices">
            {named.map(c => (
              <Voice key={c.id} c={c} s={s} />
            ))}
          </div>
        </>
      )}
      {groups.length > 0 && (
        <>
          <SecHead title="Who's this?" />
          <p className="hint voices-hint">
            Name a voice only when every request in it is the same person. One that's mixed (two people, or someone and
            the TV) is fine left as it is: TARS learns only from voices you name.
          </p>
          <div className="voices">
            {groups.map(c => (
              <Voice key={c.id} c={c} s={s} />
            ))}
          </div>
        </>
      )}
      <Folded
        title="Not sure yet"
        note="Voices heard only once. Each joins a voice above once it's heard again."
        voices={strays}
        s={s}
      />
      <Folded title="Not people" note="TV, radio and the like. TARS ignores them." voices={not} s={s} />
    </>
  );
}

// An unnamed voice heard only once is too little to name: it's folded away until it's heard again.
const GROUP_AT = 2;

function Folded({ title, note, voices, s }: { title: string; note: string; voices: Cluster[]; s: State }) {
  const [open, setOpen] = useState(false);
  if (!voices.length) return null;
  return (
    <>
      <SecHead title={title}>
        <span className="muted">{note}</span>
      </SecHead>
      {open ? (
        <div className="voices">
          {voices.map(c => (
            <Voice key={c.id} c={c} s={s} />
          ))}
        </div>
      ) : (
        <button className="voices-fold" onClick={() => setOpen(true)}>
          {`Show ${plural(voices.length, "voice")}`}
          <Icon name="chev-down" size="s" />
        </button>
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
