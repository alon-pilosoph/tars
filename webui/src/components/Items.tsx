import { useRef } from "react";
import { IKIND, fmtSize, itemName, stamp } from "../format";
import { md } from "../markdown";
import {
  type State,
  convById,
  copyItem,
  fileUrl,
  followed,
  forLabel,
  goConv,
  imageHref,
  safeUrl,
  showNote,
  showWholeList,
  tick,
  toggleMenu,
} from "../store";
import type { Item } from "../types";

const fileType = (i: Item) => {
  const e = (i.name || "").split(".").pop();
  return (e && e !== i.name ? e : (i.mime || "file").split("/").pop()!).slice(0, 4).toUpperCase();
};
const isImage = (i: Item) => /^image\//.test(i.mime || "");
const fileMeta = (i: Item) => [i.name, fmtSize(i.size)].filter(Boolean).join(", ");

type Where = "thread" | "home" | "sent";

export function ItemCard({ i, where, s }: { i: Item; where: Where; s: State }) {
  const c = convById(s, i.conversation_id),
    name = itemName(i);
  const more = useRef<HTMLButtonElement>(null); // the same item can be on the page twice (Home's strip and its thread)
  return (
    <article
      className={`item k-${i.kind}${i.seen ? "" : " unseen"}`}
      data-id={i.id}
      aria-label={`${IKIND[i.kind]}: ${name}`}
    >
      <div className="it-h">
        <span>{IKIND[i.kind]}</span>
        {i.seen ? null : <span className="new">New</span>}
        <span className="sp" />
        <time>{where === "thread" ? "" : stamp(i.ts)}</time>
        <button
          ref={more}
          className="more"
          aria-haspopup="menu"
          aria-expanded={!!more.current && s.menu?.anchor === more.current}
          aria-label={`More for ${name}`}
          onClick={e => toggleMenu({ kind: "item", id: i.id }, e.currentTarget)}
        />
      </div>
      <Body i={i} where={where} s={s} />
      {where === "thread" ? null : (
        <div className="it-f">
          <span className="for">{forLabel(s, i)}</span>
          {c && (
            <button className="tact" onClick={() => goConv(c.id)}>
              {c.speaker?.name ? `${c.speaker.name}'s` : "The"} conversation
            </button>
          )}
        </div>
      )}
    </article>
  );
}

function Body({ i, where, s }: { i: Item; where: Where; s: State }) {
  if (i.kind === "link") {
    const url = safeUrl(i.url);
    return (
      <>
        <div className="it-site">{i.site || ""}</div>
        <h3 className="it-t">
          <a href={url} target="_blank" rel="noopener" onClick={e => followed(e, i, "open")}>
            {i.title}
          </a>
        </h3>
        {i.description ? <p className="it-d">{i.description}</p> : null}
        <div className="it-a">
          <a className="btn sm" href={url} target="_blank" rel="noopener" onClick={e => followed(e, i, "open")}>
            Open ↗
          </a>
          <button className="btn sm" onClick={() => copyItem(i)}>
            Copy link
          </button>
        </div>
      </>
    );
  }
  if (i.kind === "note")
    return (
      <>
        <h3 className="it-t">{i.title}</h3>
        <div className="note-p md" dangerouslySetInnerHTML={{ __html: md(i.body || "") }} />
        <div className="it-a">
          <button className="btn sm" onClick={() => showNote(i)}>
            Open
          </button>
          <button className="btn sm" onClick={() => copyItem(i)}>
            Copy text
          </button>
        </div>
      </>
    );
  if (i.kind === "list") return <ListBody i={i} cap={where === "thread" ? 4 : 6} whole={s.lists.has(i.id)} />;
  const download = (
    <a className="btn sm" href={fileUrl(i)} download={i.name || ""} onClick={e => followed(e, i, "download")}>
      Download
    </a>
  );
  if (isImage(i))
    return (
      <>
        <a className="thumb" href={imageHref(i)} target="_blank" rel="noopener" onClick={e => followed(e, i, "open")}>
          <img src={i.preview || fileUrl(i)} alt={itemName(i)} loading="lazy" />
        </a>
        <div className="ftile">
          <div className="fn">
            <h3 className="it-t">{itemName(i)}</h3>
            <div className="fm">{fileMeta(i)}</div>
          </div>
        </div>
        <div className="it-a">{download}</div>
      </>
    );
  return (
    <>
      <div className="ftile">
        <span className="ext" aria-hidden="true">
          {fileType(i)}
        </span>
        <div className="fn">
          <h3 className="it-t">{itemName(i)}</h3>
          <div className="fm">{fileMeta(i)}</div>
        </div>
      </div>
      <div className="it-a">{download}</div>
    </>
  );
}

/** A list shows its first `cap` entries (all of them if only one more would be hidden) until "N more" is pressed. */
function ListBody({ i, cap, whole }: { i: Item; cap: number; whole: boolean }) {
  const es = i.entries || [],
    done = es.filter(e => e.done).length;
  const all = whole || es.length <= cap + 1,
    shown = all ? es : es.slice(0, cap);
  return (
    <>
      <h3 className="it-t">{i.title}</h3>
      <div className="lprog">
        <span className="bar" aria-hidden="true">
          <i style={{ width: `${es.length ? (done / es.length) * 100 : 0}%` }} />
        </span>
        {done} of {es.length} done
      </div>
      <ul className="entries">
        {shown.map((e, n) => (
          <li key={n} className={e.done ? "done" : ""}>
            <label>
              <input type="checkbox" checked={e.done} onChange={ev => tick(i.id, n, ev.target.checked)} />
              <span>{e.text}</span>
            </label>
          </li>
        ))}
      </ul>
      <div className="it-a">
        {all ? null : (
          <button className="btn sm" onClick={() => showWholeList(i.id)}>
            {es.length - cap} more
          </button>
        )}
        <button className="btn sm" onClick={() => copyItem(i)}>
          Copy list
        </button>
      </div>
    </>
  );
}
