import { useEffect, useRef } from "react";
import { fold } from "../fold";
import { plural, stamp, time } from "../format";
import {
  type State,
  cancelFix,
  expand,
  menuOpen,
  rate,
  rename,
  saveFix,
  speakerName,
  startFix,
  toggleMenu,
  turnItems,
} from "../store";
import type { Conversation as Conv, Turn } from "../types";
import { ItemCard } from "./Items";
import { PlayButton } from "./PlayButton";

function wakeLine(w: Conv["wake"]) {
  if (!w) return "";
  const parts = [
    w.heard != null ? `heard “${w.heard}”` : "",
    w.confidence != null ? `${Math.round(w.confidence * 100)}% sure` : "",
  ];
  return parts.filter(Boolean).join(", ") + (w.outcome === "ask" ? ", so TARS asked first" : "");
}

export function Conversation({ c, s }: { c: Conv; s: State }) {
  const who = speakerName(s, c.speaker) ?? "Unknown voice",
    unnamed = !c.speaker?.name;
  const { head, hidden, tail } = fold(c.turns, s.open.has(c.id), t => turnItems(s, t).length > 0 || t.id === s.edit);
  const row = (t: Turn) => <TurnRow key={t.id} t={t} c={c} first={t === c.turns[0]} s={s} />;
  return (
    <article className="conv" data-id={c.id} aria-label={`Conversation with ${who}, ${stamp(c.started)}`}>
      <header className="conv-h">
        <span className="t">{time(c.started)}</span>
        <span className={`p${unnamed ? " un" : ""}`}>{who}</span>
        {c.speaker?.cluster_id != null && unnamed ? (
          <button className="tact" onClick={() => rename(c.speaker!.cluster_id!)}>
            Name this voice
          </button>
        ) : null}
        <span className="w">{wakeLine(c.wake)}</span>
        <button
          className="more"
          aria-haspopup="menu"
          aria-expanded={menuOpen(s, "conversation", c.id)}
          aria-label="More for this conversation"
          onClick={e => toggleMenu({ kind: "conversation", id: c.id }, e.currentTarget)}
        />
      </header>
      <div className="turns">
        {head.map(row)}
        {hidden > 0 && (
          <button className="more-turns" onClick={() => expand(c.id)}>
            {plural(hidden, "more turn")}
          </button>
        )}
        {tail.map(row)}
      </div>
    </article>
  );
}

function TurnRow({ t, c, first, s }: { t: Turn; c: Conv; first: boolean; s: State }) {
  if (t.role === "tars") {
    const isAsk = c.wake?.outcome === "ask" && first;
    const items = turnItems(s, t);
    return (
      <div className="turn t" data-turn={t.id}>
        <div className="tw">
          <span className="mk" aria-hidden="true">
            <i />
            <i />
          </span>
          TARS
        </div>
        <div className="tb">
          <p className="tx">{t.text}</p>
          {isAsk ? null : (
            <div className={`ta${t.rating ? "" : " only"}`}>
              <div className={`rate${t.rating ? " rated" : ""}`} role="group" aria-label="Was this a good answer?">
                <button className="g" aria-pressed={t.rating === "good"} onClick={() => rate(t.id, "good")}>
                  {t.rating === "good" ? "✓ Good answer" : "Good"}
                </button>
                <button className="b" aria-pressed={t.rating === "bad"} onClick={() => rate(t.id, "bad")}>
                  {t.rating === "bad" ? "✗ Bad answer" : "Bad"}
                </button>
              </div>
            </div>
          )}
          {items.length ? (
            <div className="titems">
              {items.map(i => (
                <ItemCard key={i.id} i={i} where="thread" s={s} />
              ))}
            </div>
          ) : null}
        </div>
      </div>
    );
  }
  const name = speakerName(s, t.speaker) || speakerName(s, c.speaker) || "Someone";
  const text = t.corrected_text || t.text,
    aside = !!t.not_for_tars;
  const play = t.has_audio ? <PlayButton clip={{ turn: t.id }} label={`what ${name} said`} sm /> : null;
  if (s.edit === t.id)
    return (
      <div className="turn p editing" data-turn={t.id}>
        <div className="tw">{name}</div>
        <div className="tb">
          <div className="tl">
            {play}
            <Editor t={t} text={text} />
          </div>
        </div>
      </div>
    );
  const notes = aside || t.corrected_text;
  return (
    <div className={`turn p${aside ? " aside" : ""}`} data-turn={t.id}>
      <div className="tw">{name}</div>
      <div className="tb">
        <div className="tl">
          {play}
          <p className="tx">“{text}”</p>
        </div>
        <div className={`ta${notes ? "" : " only"}`}>
          {aside ? <span>Not for TARS, so it stayed quiet</span> : null}
          {t.corrected_text ? (
            <span className="fixed">
              Corrected from <s>“{t.text}”</s>
            </span>
          ) : null}
          <button className="tact" onClick={() => startFix(t.id)}>
            {t.corrected_text ? "Edit fix" : "Fix text"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Correcting what TARS heard. Enter saves and Escape cancels, inside the box only. */
function Editor({ t, text }: { t: Turn; text: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const ta = ref.current!;
    ta.focus({ preventScroll: true });
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }, []);
  const save = () => saveFix(t.id, ref.current?.value || "");
  return (
    <div className="edit" style={{ flex: 1, minWidth: 0 }}>
      <label className="sr" htmlFor={`fx-${t.id}`}>
        What was said
      </label>
      <textarea
        ref={ref}
        id={`fx-${t.id}`}
        rows={2}
        defaultValue={text}
        onKeyDown={e => {
          if (e.nativeEvent.isComposing) return;
          if (e.key === "Escape") cancelFix();
          else if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            save();
          }
        }}
      />
      <div className="row">
        <button className="btn sm primary" onClick={save}>
          Save
        </button>
        <button className="btn sm" onClick={cancelFix}>
          Cancel
        </button>
        <span className="hint">TARS heard “{t.text}”</span>
      </div>
    </div>
  );
}
