import { Fragment, type MouseEvent, useEffect, useRef } from "react";
import { failedLine, stamp, time, timingLine, wakeLine } from "../format";
import {
  type State,
  cancelFix,
  copyTranscript,
  delConv,
  menuOpen,
  rate,
  rename,
  saveFix,
  showMoreTurns,
  speakerName,
  startFix,
  takeFixFocus,
  toggleMenu,
  toggleOpen,
  toggleWakeNotForTars,
  turnItems,
} from "../store";
import type { Conversation, Speaker, Turn } from "../types";
import { Icon } from "./Icon";
import { ItemCard, SentLink } from "./Items";
import { PlaySmall, Player } from "./PlayButton";

type Unit = { turn: Turn } | { aside: Turn[] };

function units(c: Conversation): Unit[] {
  const out: Unit[] = [];
  for (const t of c.turns) {
    const last = out[out.length - 1];
    if (t.role === "person" && t.not_for_tars) {
      if (last && "aside" in last) last.aside.push(t);
      else out.push({ aside: [t] });
    } else out.push({ turn: t });
  }
  return out;
}

const said = (t: Turn) => t.corrected_text || t.text;
const stop = (e: MouseEvent) => e.stopPropagation();
const isUnknown = (sp: Speaker | null | undefined) => !!sp && !sp.name && sp.cluster_id != null;

function asideLabel(s: State, c: Conversation, turns: Turn[]) {
  const names = [...new Set(turns.map(t => speakerName(s, t.speaker ?? c.speaker) || "Someone"))];
  if (names.length < 2) return `${names[0]}, not to TARS`;
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}, to each other`;
}

const rateable = (c: Conversation, t: Turn) => !(c.wake?.outcome === "ask" && t === c.turns[0]);

export function ConversationRow({ c, s }: { c: Conversation; s: State }) {
  const open = s.open.has(c.id);
  return (
    <article className="conv" data-id={c.id} aria-label={`Conversation, ${stamp(c.started)}`}>
      <div className="conv-time">{time(c.started)}</div>
      {open ? <Panel c={c} s={s} /> : <Closed c={c} s={s} />}
    </article>
  );
}

function Who({ c, t, s }: { c: Conversation; t: Turn; s: State }) {
  const sp = t.speaker ?? c.speaker;
  const unknown = isUnknown(sp) ? sp!.cluster_id : null;
  return (
    <>
      <b>{speakerName(s, sp) || "Someone"}</b>
      {unknown != null && (
        <>
          {" "}
          <button
            className="name-link"
            onClick={e => {
              stop(e);
              rename(unknown);
            }}
          >
            Name this voice
          </button>
        </>
      )}
    </>
  );
}

function Rate({ t }: { t: Turn }) {
  return (
    <div className="rate" role="group" aria-label="Was this a good answer?">
      {(["good", "bad"] as const).map(v => (
        <button
          key={v}
          className={`icon-btn rate-${v}`}
          aria-label={v === "good" ? "Good answer" : "Bad answer"}
          aria-pressed={t.rating === v}
          onClick={e => {
            stop(e);
            rate(t.id, v);
          }}
        >
          <Icon name={v === "good" ? "up" : "down"} />
        </button>
      ))}
    </div>
  );
}

function Closed({ c, s }: { c: Conversation; s: State }) {
  let shown: (Unit | { more: number })[] = units(c);
  if (shown.length > 4 && !s.moreTurns.has(c.id))
    shown = [shown[0], shown[1], { more: shown.length - 3 }, shown[shown.length - 1]];
  return (
    <div
      className="conv-body tap"
      role="button"
      tabIndex={0}
      aria-expanded={false}
      aria-label={`Open the conversation, ${stamp(c.started)}`}
      onClick={() => toggleOpen(c.id)}
      onKeyDown={e => {
        if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          toggleOpen(c.id);
        }
      }}
    >
      {shown.map((u, n) => {
        if ("more" in u)
          return (
            <button
              key="more"
              className="more-turns"
              onClick={e => {
                stop(e);
                showMoreTurns(c.id);
              }}
            >
              {`${u.more} more turns`}
              <Icon name="chev-down" size="s" />
            </button>
          );
        if ("aside" in u)
          return (
            <div key={n} className="aside">
              <span className="lbl">{asideLabel(s, c, u.aside)}</span>
              {u.aside.map((t, k) => (
                <Fragment key={t.id}>
                  {k > 0 && <br />}
                  {said(t)}
                </Fragment>
              ))}
            </div>
          );
        const t = u.turn;
        if (t.role === "person")
          return (
            <div key={t.id} className="turn" data-turn={t.id}>
              {t.has_audio && <PlaySmall clip={{ turn: t.id }} label="what was said" />}
              <div className="said">
                <Who c={c} t={t} s={s} />
                {` ${said(t)}`}
              </div>
            </div>
          );
        const items = turnItems(s, t);
        return (
          <div key={t.id} className="tars-unit" data-turn={t.id}>
            <div className="turn tars">
              <div className="said tars">{t.text}</div>
              {rateable(c, t) && <Rate t={t} />}
            </div>
            {t.failed_at && (
              <div className="fail">
                <Icon name="alert" size="s" />
                {failedLine(t)}
              </div>
            )}
            {items.length > 0 && (
              <div className="sent-links">
                {items.map(i => (
                  <SentLink key={i.id} i={i} />
                ))}
              </div>
            )}
          </div>
        );
      })}
      {c.wake?.label === "not_real" && <div className="labelled-not indented">Marked as not meant for TARS.</div>}
    </div>
  );
}

function Panel({ c, s }: { c: Conversation; s: State }) {
  const notReal = c.wake?.label === "not_real";
  const who = { kind: "who", id: c.id } as const;
  const name = speakerName(s, c.speaker);
  return (
    <div className="panel">
      <div className="panel-head">
        <span className="who">{`${name || "Someone"}, ${stamp(c.started)}`}</span>
        <button className="icon-btn" aria-label="Close the conversation" onClick={() => toggleOpen(c.id)}>
          <Icon name="chev-up" />
        </button>
      </div>
      {units(c).map((u, n) => {
        if ("aside" in u)
          return (
            <div key={n} className="p-turn">
              <div className="aside flush">
                <span className="lbl">{asideLabel(s, c, u.aside)}</span>
                {u.aside.map(t => (
                  <span key={t.id} className="aside-line">
                    {t.has_audio && <PlaySmall clip={{ turn: t.id }} label="what was said" />}
                    <span>{said(t)}</span>
                  </span>
                ))}
              </div>
            </div>
          );
        const t = u.turn;
        if (t.role === "person")
          return (
            <div key={t.id} className="p-turn" data-turn={t.id}>
              {s.edit === t.id ? (
                <Editor t={t} />
              ) : (
                <>
                  <div className="said">
                    <Who c={c} t={t} s={s} />
                    {` ${said(t)}`}
                  </div>
                  {t.corrected_text && <div className="fixed-note">{`Fixed. TARS first heard “${t.text}”`}</div>}
                  <div className="p-row">
                    {t.has_audio && <Player clip={{ turn: t.id }} label="what was said" />}
                    <button className="btn text-action" onClick={() => startFix(t.id)}>
                      <Icon name="edit" size="s" />
                      Fix text
                    </button>
                  </div>
                </>
              )}
            </div>
          );
        const info = timingLine(t);
        return (
          <div key={t.id} className="p-turn" data-turn={t.id}>
            <div className="said tars">{t.text}</div>
            {t.failed_at && (
              <div className="fail">
                <Icon name="alert" size="s" />
                {failedLine(t)}
              </div>
            )}
            {rateable(c, t) && (
              <div className="btns" role="group" aria-label="Was this a good answer?">
                <button className="btn rate-good" aria-pressed={t.rating === "good"} onClick={() => rate(t.id, "good")}>
                  <Icon name="up" size="s" />
                  Good
                </button>
                <button className="btn rate-bad" aria-pressed={t.rating === "bad"} onClick={() => rate(t.id, "bad")}>
                  <Icon name="down" size="s" />
                  Bad
                </button>
              </div>
            )}
            {turnItems(s, t).map(i => (
              <ItemCard key={i.id} i={i} s={s} />
            ))}
            {info && <div className="p-info">{info}</div>}
          </div>
        );
      })}
      <div className="p-foot">
        <div className="p-info">
          {wakeLine(c.wake, { name: c.speaker?.name ?? null, known: !isUnknown(c.speaker) }) +
            (notReal ? " Marked as not meant for TARS." : "")}
        </div>
        <div className="p-actions">
          <button className="btn ghost" onClick={() => copyTranscript(c)}>
            <Icon name="copy" size="s" />
            Copy
          </button>
          {c.wake && (
            <button className="btn ghost" onClick={() => toggleWakeNotForTars(c)}>
              <Icon name="notfor" size="s" />
              {notReal ? "Meant for TARS after all" : "Not meant for TARS"}
            </button>
          )}
          {c.wake?.has_request_audio && (
            <div className="anchor">
              <button
                className="btn ghost"
                aria-haspopup="menu"
                aria-expanded={menuOpen(s, who)}
                onClick={e => toggleMenu(who, e.currentTarget)}
              >
                <Icon name="person" size="s" />
                Who was talking
              </button>
            </div>
          )}
          <button className="btn ghost" onClick={() => delConv(c.id)}>
            <Icon name="trash" size="s" />
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

function Editor({ t }: { t: Turn }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const area = ref.current;
    if (!area || !takeFixFocus()) return;
    area.focus({ preventScroll: true });
    area.setSelectionRange(area.value.length, area.value.length);
  }, []);
  const save = () => saveFix(t.id, ref.current?.value || "");
  return (
    <div className="edit">
      <label className="sr" htmlFor={`fix-${t.id}`}>
        What was said
      </label>
      <textarea
        ref={ref}
        id={`fix-${t.id}`}
        rows={3}
        defaultValue={said(t)}
        onKeyDown={e => {
          if (e.nativeEvent.isComposing) return;
          if (e.key === "Escape") cancelFix();
          else if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            save();
          }
        }}
      />
      <div className="btns">
        <button className="btn primary" onClick={save}>
          Save
        </button>
        <button className="btn ghost" onClick={cancelFix}>
          Cancel
        </button>
        <span className="hint">Enter saves, Escape cancels.</span>
      </div>
    </div>
  );
}
