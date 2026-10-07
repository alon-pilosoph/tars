import { useEffect, useRef, useState } from "react";
import { stamp } from "../format";
import { BOLD, md } from "../markdown";
import {
  type DialogState,
  type State,
  clusterById,
  clusterName,
  copyItem,
  download,
  get,
  merge,
  mergeKeeps,
  set,
  useStore,
} from "../store";
import { Icon } from "./Icon";
import { forLine } from "./Items";
import { PlaySmall } from "./PlayButton";

/** Splitting on BOLD's capture group puts the bold parts at the odd indices. */
const rich = (text: string) => text.split(BOLD).map((part, i) => (i % 2 ? <b key={i}>{part}</b> : part));

export function Dialog() {
  const s = useStore();
  const d = s.dialog;
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const ok = useRef<HTMLButtonElement>(null);
  const no = useRef<HTMLButtonElement>(null);
  const downOutside = useRef(false);
  // Kept with the dialog it was typed in, so a new prompt starts from its own value.
  const [typed, setTyped] = useState<{ dialog: DialogState; value: string } | null>(null);
  const value = typed && typed.dialog === d ? typed.value : d?.kind === "prompt" ? (d.value ?? "") : "";

  useEffect(() => {
    const el = ref.current;
    if (!d || !el || el.open) return;
    el.returnValue = "";
    el.showModal();
    // A delete opens on Cancel, so Enter never deletes by accident.
    (d.kind === "prompt" ? input.current : d.kind === "confirm" && d.danger ? no.current : ok.current)?.focus();
  }, [d]);

  const closed = () => {
    const cur = get().dialog;
    if (!cur) return;
    const yes = ref.current?.returnValue === "ok";
    set({ dialog: null });
    if (cur.kind === "confirm") cur.resolve(yes);
    if (cur.kind === "prompt") cur.resolve(yes ? (input.current?.value ?? "").trim() : null);
  };
  const cancel = () => ref.current?.close("cancel");
  const wide = d?.kind === "note" || d?.kind === "image";
  const title =
    d?.kind === "note" || d?.kind === "image"
      ? d.item.title
      : d?.kind === "merge"
        ? `Merge ${clusterName(s, d.id)} with…`
        : (d?.title ?? "");

  // A click on the backdrop cancels, but not the end of a text selection that was dragged out of the dialog.
  return (
    <dialog
      ref={ref}
      className={wide ? "dialog wide" : "dialog"}
      aria-labelledby="dialog-title"
      onClose={closed}
      onPointerDown={e => {
        downOutside.current = e.target === ref.current;
      }}
      onClick={e => {
        if (e.target === ref.current && downOutside.current) cancel();
      }}
    >
      <form method="dialog" className="dialog-form">
        <div className="grabber" />
        <button type="button" className="icon-btn close" aria-label="Close" onClick={cancel}>
          <Icon name="close" />
        </button>
        <h2 id="dialog-title">{title}</h2>
        {d?.kind === "note" || d?.kind === "image" ? (
          <>
            <div className="kind-line">
              <Icon name={d.kind === "note" ? "note" : "image"} size="s" />
              <span>{`${d.kind === "note" ? "Note" : "Photo"} ${forLine(s, d.item)}, ${stamp(d.item.ts)}`}</span>
            </div>
            {d.kind === "note" ? (
              <div className="prose" dangerouslySetInnerHTML={{ __html: md(d.item.body || "") }} />
            ) : (
              <img className="preview full" src={d.item.preview || d.item.url || ""} alt={d.item.title} />
            )}
            <div className="btns">
              {d.kind === "note" ? (
                <button type="button" className="btn ghost big" onClick={() => copyItem(d.item)}>
                  <Icon name="copy" size="s" />
                  Copy text
                </button>
              ) : (
                <button type="button" className="btn ghost big" onClick={() => download(d.item)}>
                  <Icon name="download" size="s" />
                  Download
                </button>
              )}
              <button ref={ok} className="btn primary big" value="cancel">
                Done
              </button>
            </div>
          </>
        ) : d?.kind === "merge" ? (
          <Merge s={s} id={d.id} cancel={cancel} />
        ) : (
          d && (
            <>
              {d.text && <p>{rich(d.text)}</p>}
              {d.kind === "prompt" && (
                <>
                  <label className="field">
                    <span className="lbl">Name</span>
                    <input
                      ref={input}
                      type="text"
                      placeholder={d.placeholder}
                      value={value}
                      maxLength={d.maxLength}
                      autoComplete="off"
                      onChange={e => setTyped({ dialog: d, value: e.currentTarget.value })}
                    />
                  </label>
                  {d.samples?.length ? (
                    <div className="samples">
                      {d.samples.map(x => (
                        <div key={x.event_id} className="sample">
                          <PlaySmall clip={{ event: x.event_id, part: "request" }} label="a request in this voice" />
                          <span>{`“${x.transcript || "…"}”`}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </>
              )}
              <div className="btns">
                {/* type="button": Enter in the name box submits with the first submit button, which must be OK */}
                <button ref={no} type="button" className="btn ghost big" onClick={cancel}>
                  {(d.kind === "confirm" && d.no) || "Cancel"}
                </button>
                <button
                  ref={ok}
                  className="btn primary big"
                  value="ok"
                  disabled={d.kind === "prompt" && d.required && !value.trim()}
                >
                  {d.ok ?? "OK"}
                </button>
              </div>
            </>
          )
        )}
      </form>
    </dialog>
  );
}

function Merge({ s, id, cancel }: { s: State; id: number; cancel: () => void }) {
  const a = clusterById(s, id);
  const others = s.clusters.filter(c => c.id !== id && c.kind !== "not_person");
  const [other, setOther] = useState<number | null>(others.find(c => c.name)?.id ?? null);
  const [keep, setKeep] = useState<number | null>(null);
  if (!a) return null;
  const b = other != null ? clusterById(s, other) : undefined;
  const kept = b ? (keep ?? mergeKeeps(s, a.id, b.id)) : null;
  return (
    <>
      <p>Their requests become one voice. TARS relearns it from all of them.</p>
      <div className="choices" role="radiogroup" aria-label="The other voice">
        {others.map(c => (
          <button
            type="button"
            key={c.id}
            className="choice"
            role="radio"
            aria-checked={other === c.id}
            onClick={() => {
              setOther(c.id);
              setKeep(null);
            }}
          >
            <span className="radio" />
            <span>
              {clusterName(s, c.id)}
              <small>{`${c.size} requests`}</small>
            </span>
          </button>
        ))}
      </div>
      {b && a.name && b.name && (
        <div className="field">
          <span className="lbl">Keep which name?</span>
          <div className="pills">
            {[a, b].map(c => (
              <button
                type="button"
                key={c.id}
                className="btn"
                aria-pressed={kept === c.id}
                onClick={() => setKeep(c.id)}
              >
                {c.name}
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="btns">
        <button type="button" className="btn ghost big" onClick={cancel}>
          Cancel
        </button>
        <button
          type="button"
          className="btn primary big"
          disabled={!b}
          onClick={() => {
            if (!b || kept == null) return;
            cancel();
            merge(kept, kept === a.id ? b.id : a.id);
          }}
        >
          Merge
        </button>
      </div>
    </>
  );
}
