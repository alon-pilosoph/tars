import { useEffect, useRef } from "react";
import { fold } from "../fold";
import { heardText, plural, stamp, time } from "../format";
import {
  type MenuTarget,
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
import type { Conversation, ConversationWake, Turn } from "../types";
import { ItemCard } from "./Items";
import { PlayButton } from "./PlayButton";

function wakeLine(w: ConversationWake | null) {
  if (!w) return "";
  const parts = [
    w.heard != null ? `heard “${heardText(w.heard)}”` : "",
    w.confidence != null ? `${Math.round(w.confidence * 100)}% sure` : "",
  ];
  return parts.filter(Boolean).join(", ") + (w.outcome === "ask" ? ", so TARS asked first" : "");
}

export function ConversationCard({ c, s }: { c: Conversation; s: State }) {
  const who = speakerName(s, c.speaker) ?? "Unknown voice";
  const unnamed = !c.speaker?.name;
  const unnamedVoice = unnamed ? c.speaker?.cluster_id : null;
  const { head, hidden, tail } = fold(c.turns, s.open.has(c.id), t => turnItems(s, t).length > 0 || t.id === s.edit);
  const row = (t: Turn) => <TurnRow key={t.id} t={t} c={c} first={t === c.turns[0]} s={s} />;
  const menu: MenuTarget = { kind: "conversation", id: c.id };
  return (
    <article className="conv" data-id={c.id} aria-label={`Conversation with ${who}, ${stamp(c.started)}`}>
      <header className="conv-head">
        <span className="conv-time">{time(c.started)}</span>
        <span className={`conv-who${unnamed ? " unnamed" : ""}`}>{who}</span>
        {unnamedVoice != null ? (
          <button className="text-action" onClick={() => rename(unnamedVoice)}>
            Name this voice
          </button>
        ) : null}
        <span className="conv-wake">{wakeLine(c.wake)}</span>
        <button
          className="more"
          aria-haspopup="menu"
          aria-expanded={menuOpen(s, menu)}
          aria-label="More for this conversation"
          onClick={e => toggleMenu(menu, e.currentTarget)}
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

function TurnRow({ t, c, first, s }: { t: Turn; c: Conversation; first: boolean; s: State }) {
  if (t.role === "tars") {
    const isAsk = c.wake?.outcome === "ask" && first;
    const items = turnItems(s, t);
    return (
      <div className="turn tars" data-turn={t.id}>
        <div className="turn-who">
          <span className="turn-mark" aria-hidden="true">
            <i />
            <i />
          </span>
          TARS
        </div>
        <div className="turn-body">
          <p className="turn-text">{t.text}</p>
          {isAsk ? null : (
            <div className="turn-actions">
              <div className="rate" role="group" aria-label="Was this a good answer?">
                <button className="rate-good" aria-pressed={t.rating === "good"} onClick={() => rate(t.id, "good")}>
                  {t.rating === "good" ? "✓ Good" : "Good"}
                </button>
                <button className="rate-bad" aria-pressed={t.rating === "bad"} onClick={() => rate(t.id, "bad")}>
                  {t.rating === "bad" ? "✗ Bad" : "Bad"}
                </button>
              </div>
            </div>
          )}
          {items.length ? (
            <div className="turn-items">
              {items.map(i => (
                <ItemCard key={i.id} i={i} place="thread" s={s} />
              ))}
            </div>
          ) : null}
        </div>
      </div>
    );
  }
  const name = speakerName(s, t.speaker) || speakerName(s, c.speaker) || "Someone";
  const text = t.corrected_text || t.text;
  const aside = !!t.not_for_tars;
  const play = t.has_audio ? <PlayButton clip={{ turn: t.id }} label={`what ${name} said`} small /> : null;
  if (s.edit === t.id)
    return (
      <div className="turn person" data-turn={t.id}>
        <div className="turn-who">{name}</div>
        <div className="turn-body">
          <div className="turn-line">
            {play}
            <Editor t={t} text={text} />
          </div>
        </div>
      </div>
    );
  return (
    <div className={`turn person${aside ? " aside" : ""}`} data-turn={t.id}>
      <div className="turn-who">{name}</div>
      <div className="turn-body">
        <div className="turn-line">
          {play}
          <p className="turn-text">“{text}”</p>
        </div>
        <div className="turn-actions">
          {aside ? <span>Not for TARS, so it stayed quiet</span> : null}
          {t.corrected_text ? (
            <span className="fixed">
              Corrected from <s>“{t.text}”</s>
            </span>
          ) : null}
          <button className="text-action" onClick={() => startFix(t.id)}>
            {t.corrected_text ? "Edit fix" : "Fix text"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Editor({ t, text }: { t: Turn; text: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const area = ref.current;
    if (!area) return;
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
