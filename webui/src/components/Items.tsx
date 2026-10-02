import { COPY_LABEL, ITEM_KIND_LABEL, fmtSize, itemName, stamp } from "../format";
import { md } from "../markdown";
import {
  type ItemPlace,
  type MenuTarget,
  type State,
  convById,
  copyItem,
  followed,
  forLabel,
  goConv,
  menuOpen,
  safeUrl,
  showItem,
  showWholeList,
  tick,
  toggleMenu,
} from "../store";
import type { Item } from "../types";

function fileType(i: Item) {
  const extension = (i.name || "").split(".").pop();
  const type = extension && extension !== i.name ? extension : (i.mime || "file").split("/").pop() || "file";
  return type.slice(0, 4).toUpperCase();
}
const isImage = (i: Item) => (i.mime || "").startsWith("image/");
const fileMeta = (i: Item) => [i.name, fmtSize(i.size)].filter(Boolean).join(", ");

export function ItemCard({ i, place, s }: { i: Item; place: ItemPlace; s: State }) {
  const c = convById(s, i.conversation_id);
  const name = itemName(i);
  const menu: MenuTarget = { kind: "item", id: i.id, place };
  const otherSpeaker = c?.speaker?.name && c.speaker.cluster_id !== i.for?.cluster_id ? c.speaker.name : null;
  return (
    <article
      className={`item${i.seen ? "" : " unseen"}`}
      data-id={i.id}
      aria-label={`${ITEM_KIND_LABEL[i.kind]}: ${name}`}
    >
      <div className="item-head">
        <span>{ITEM_KIND_LABEL[i.kind]}</span>
        {i.seen ? null : <span className="new">New</span>}
        <span className="spacer" />
        <time>{place === "thread" ? "" : stamp(i.ts)}</time>
        <button
          className="more"
          aria-haspopup="menu"
          aria-expanded={menuOpen(s, menu)}
          aria-label={`More for ${name}`}
          onClick={e => toggleMenu(menu, e.currentTarget)}
        />
      </div>
      <Body i={i} place={place} s={s} />
      {place === "thread" ? null : (
        <div className="item-foot">
          <span className="item-for">{forLabel(s, i)}</span>
          {c && (
            <button className="text-action" onClick={() => goConv(c.id)}>
              {otherSpeaker ? `${otherSpeaker}'s` : "The"} conversation
            </button>
          )}
        </div>
      )}
    </article>
  );
}

function Body({ i, place, s }: { i: Item; place: ItemPlace; s: State }) {
  if (i.kind === "link") {
    const url = safeUrl(i.url);
    return (
      <>
        <div className="item-site">{i.site || ""}</div>
        <h3 className="item-title">
          <a href={url} target="_blank" rel="noopener noreferrer" onClick={() => followed(i)}>
            {i.title}
          </a>
        </h3>
        {i.description ? <p className="item-desc">{i.description}</p> : null}
        <div className="item-actions">
          <a className="btn sm" href={url} target="_blank" rel="noopener noreferrer" onClick={() => followed(i)}>
            Open ↗
          </a>
          <button className="btn sm" onClick={() => copyItem(i)}>
            {COPY_LABEL.link}
          </button>
        </div>
      </>
    );
  }
  if (i.kind === "note")
    return (
      <>
        <h3 className="item-title">{i.title}</h3>
        <div className="note-preview md" dangerouslySetInnerHTML={{ __html: md(i.body || "") }} />
        <div className="item-actions">
          <button className="btn sm" onClick={() => showItem(i)}>
            Open
          </button>
          <button className="btn sm" onClick={() => copyItem(i)}>
            {COPY_LABEL.note}
          </button>
        </div>
      </>
    );
  if (i.kind === "list") return <ListBody i={i} cap={place === "thread" ? 4 : 6} whole={s.lists.has(i.id)} />;
  const download = (
    <a className="btn sm" href={i.url ?? undefined} download={i.name || ""} onClick={() => followed(i)}>
      Download
    </a>
  );
  const tile = (
    <div className="file-name">
      <h3 className="item-title">{itemName(i)}</h3>
      <div className="file-meta">{fileMeta(i)}</div>
    </div>
  );
  if (isImage(i))
    return (
      <>
        <button className="thumb" aria-label={`Show ${itemName(i)}`} onClick={() => showItem(i)}>
          <img src={i.preview || i.url || undefined} alt={itemName(i)} loading="lazy" />
        </button>
        <div className="file-tile">{tile}</div>
        <div className="item-actions">{download}</div>
      </>
    );
  return (
    <>
      <div className="file-tile">
        <span className="file-ext" aria-hidden="true">
          {fileType(i)}
        </span>
        {tile}
      </div>
      <div className="item-actions">{download}</div>
    </>
  );
}

function ListBody({ i, cap, whole }: { i: Item; cap: number; whole: boolean }) {
  const entries = i.entries || [];
  const done = entries.filter(e => e.done).length;
  const all = whole || entries.length <= cap + 1;
  const shown = all ? entries : entries.slice(0, cap);
  const percentDone = entries.length ? (done / entries.length) * 100 : 0;
  return (
    <>
      <h3 className="item-title">{i.title}</h3>
      <div className="list-progress">
        <span className="bar" aria-hidden="true">
          <i style={{ width: `${percentDone}%` }} />
        </span>
        {done} of {entries.length} done
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
      <div className="item-actions">
        {all ? null : (
          <button className="btn sm" onClick={() => showWholeList(i.id)}>
            {entries.length - cap} more
          </button>
        )}
        <button className="btn sm" onClick={() => copyItem(i)}>
          {COPY_LABEL.list}
        </button>
      </div>
    </>
  );
}
