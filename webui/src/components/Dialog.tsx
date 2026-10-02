import { useEffect, useRef, useState } from "react";
import { when } from "../format";
import { BOLD, md } from "../markdown";
import { type DialogState, copyItem, download, forLabel, get, set, useStore } from "../store";

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

  // A click on the backdrop cancels, but not the end of a text selection that was dragged out of the dialog.
  return (
    <dialog
      ref={ref}
      className={wide ? "wide" : ""}
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
        {d?.kind === "note" || d?.kind === "image" ? (
          <>
            <h3 id="dialog-title">{d.item.title}</h3>
            <div className="dialog-sub">
              {forLabel(s, d.item)}, sent {when(d.item.ts)}
            </div>
            {d.kind === "note" ? (
              <div className="md" dangerouslySetInnerHTML={{ __html: md(d.item.body || "") }} />
            ) : (
              <img className="dialog-image" src={d.item.preview || d.item.url || ""} alt={d.item.title} />
            )}
            <div className="row">
              {d.kind === "note" ? (
                <button type="button" className="btn" onClick={() => copyItem(d.item)}>
                  Copy text
                </button>
              ) : (
                <button type="button" className="btn" onClick={() => download(d.item)}>
                  Download
                </button>
              )}
              <button ref={ok} className="btn primary" value="cancel">
                Done
              </button>
            </div>
          </>
        ) : (
          d && (
            <>
              <h3 id="dialog-title">{d.title}</h3>
              {d.text && <p>{rich(d.text)}</p>}
              {d.kind === "prompt" && (
                <input
                  ref={input}
                  placeholder={d.placeholder}
                  value={value}
                  maxLength={d.maxLength}
                  autoComplete="off"
                  onChange={e => setTyped({ dialog: d, value: e.currentTarget.value })}
                />
              )}
              <div className="row">
                {/* type="button": Enter in the name box submits with the first submit button, which must be OK */}
                <button ref={no} type="button" className="btn" value="cancel" onClick={cancel}>
                  Cancel
                </button>
                <button
                  ref={ok}
                  className={`btn ${d.kind === "confirm" && d.danger ? "danger" : "primary"}`}
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
