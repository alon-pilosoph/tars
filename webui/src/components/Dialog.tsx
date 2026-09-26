import { useEffect, useRef } from "react";
import { when } from "../format";
import { md } from "../markdown";
import { copyItem, forLabel, get, set, useStore } from "../store";

/** "Everything in **Voice 3** moves…" with the names in bold. */
const rich = (text: string) => text.split(/\*\*(.+?)\*\*/).map((part, i) => (i % 2 ? <b key={i}>{part}</b> : part));

/** The one modal: confirmations, name prompts, and an opened note (wide). */
export function Dialog() {
  const s = useStore(),
    d = s.dialog;
  const ref = useRef<HTMLDialogElement>(null),
    input = useRef<HTMLInputElement>(null),
    ok = useRef<HTMLButtonElement>(null);
  const downOutside = useRef(false);

  useEffect(() => {
    const el = ref.current!;
    if (!d || el.open) return;
    el.returnValue = "";
    el.showModal();
    (d.kind === "prompt" ? input.current : ok.current)?.focus();
  }, [d]);

  const closed = () => {
    const cur = get().dialog;
    if (!cur) return;
    const yes = ref.current!.returnValue === "ok",
      value = input.current?.value.trim() ?? "";
    set({ dialog: null });
    if (cur.kind === "confirm") cur.resolve(yes);
    if (cur.kind === "prompt") cur.resolve(yes ? value : null);
  };
  const cancel = () => ref.current!.close("cancel");

  // A click on the backdrop cancels, but not the end of a text selection that was dragged out of the dialog.
  return (
    <dialog
      ref={ref}
      className={d?.kind === "note" ? "wide" : ""}
      aria-labelledby="dlg-t"
      onClose={closed}
      onPointerDown={e => {
        downOutside.current = e.target === ref.current;
      }}
      onClick={e => {
        if (e.target === ref.current && downOutside.current) cancel();
      }}
    >
      <form method="dialog" className="dlg">
        {d?.kind === "note" ? (
          <>
            <h3 id="dlg-t">{d.item.title}</h3>
            <div className="sub">
              {forLabel(s, d.item)}, sent {when(d.item.ts)}
            </div>
            <div className="md" dangerouslySetInnerHTML={{ __html: md(d.item.body || "") }} />
            <div className="row">
              <button type="button" className="btn" onClick={() => copyItem(d.item)}>
                Copy text
              </button>
              <button ref={ok} className="btn primary" value="cancel">
                Done
              </button>
            </div>
          </>
        ) : (
          d && (
            <>
              <h3 id="dlg-t">{d.title}</h3>
              {d.text && <p>{rich(d.text)}</p>}
              {d.kind === "prompt" && (
                <input ref={input} placeholder={d.placeholder} defaultValue={d.value ?? ""} autoComplete="off" />
              )}
              <div className="row">
                {/* type="button": Enter in the name box submits with the first submit button, which must be OK */}
                <button type="button" className="btn" value="cancel" onClick={cancel}>
                  Cancel
                </button>
                <button
                  ref={ok}
                  className={`btn ${d.kind === "confirm" && d.danger ? "danger" : "primary"}`}
                  value="ok"
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
