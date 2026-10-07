import { COPY_LABEL, fmtSize, itemName, stamp, time } from "../format";
import { md } from "../markdown";
import { linkTo } from "../params";
import {
  type State,
  clusterName,
  convById,
  copyItem,
  followed,
  goConv,
  goItem,
  menuOpen,
  safeUrl,
  showItem,
  speakerName,
  tick,
  toggleMenu,
  toggleWholeList,
} from "../store";
import type { Item } from "../types";
import { Icon, type IconName } from "./Icon";

const isImage = (i: Item) => (i.mime || "").startsWith("image/");
const kindIcon = (i: Item): IconName => (i.kind === "file" ? (isImage(i) ? "image" : "file") : i.kind);
const kindWord = (i: Item) =>
  i.kind === "file" ? (isImage(i) ? "Photo" : "File") : { link: "Link", note: "Note", list: "List" }[i.kind];

export function forLine(s: State, i: Item) {
  if (i.scope === "household") return "for the household";
  return `for ${i.for?.name || clusterName(s, i.for?.cluster_id) || "whoever asked"}`;
}

function fileType(i: Item) {
  const extension = (i.name || "").split(".").pop();
  const type = extension && extension !== i.name ? extension : (i.mime || "file").split("/").pop() || "file";
  return type.slice(0, 4).toUpperCase();
}

const New = ({ i }: { i: Item }) => (i.seen ? null : <span className="new">New</span>);

export function NewRow({ i, s }: { i: Item; s: State }) {
  return (
    <a
      className="row"
      href={linkTo({ tab: "sent", item: i.id })}
      onClick={e => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        goItem(i.id);
      }}
    >
      <span className="thumb">
        {isImage(i) && i.preview ? <img src={i.preview} alt="" /> : <Icon name={kindIcon(i)} />}
      </span>
      <span>
        <span className="row-title">{itemName(i)}</span>
        <span className="row-meta">
          {`${kindWord(i)} ${forLine(s, i)}, ${time(i.ts)} `}
          <New i={i} />
        </span>
      </span>
      <svg className="icon chev" viewBox="0 0 24 24" aria-hidden="true">
        <path d="m9 6 6 6-6 6" />
      </svg>
    </a>
  );
}

export function SentLink({ i }: { i: Item }) {
  return (
    <a
      href={linkTo({ tab: "sent", item: i.id })}
      onClick={e => {
        e.stopPropagation();
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        goItem(i.id);
      }}
    >
      <Icon name={kindIcon(i)} size="s" />
      {itemName(i)}
    </a>
  );
}

export function ItemCard({ i, s }: { i: Item; s: State }) {
  return (
    <div className="card" data-id={i.id}>
      <div className="kind-line">
        <Icon name={kindIcon(i)} size="s" />
        <span>{`${kindWord(i)} ${forLine(s, i)}`}</span>
        <New i={i} />
      </div>
      <Title i={i} />
      <Body i={i} s={s} compact />
      <Actions i={i} s={s} compact />
    </div>
  );
}

export function SentItem({ i, s }: { i: Item; s: State }) {
  const c = convById(s, i.conversation_id);
  const name = itemName(i);
  return (
    <article className="item" data-id={i.id} aria-label={`${kindWord(i)}: ${name}`}>
      <div className="kind-line">
        <Icon name={kindIcon(i)} size="s" />
        <span>{`${kindWord(i)} ${forLine(s, i)}, ${stamp(i.ts).replace(/^Today, /, "")}`}</span>
        <New i={i} />
        <span className="grow" />
        <div className="anchor">
          <button
            className="icon-btn"
            aria-haspopup="menu"
            aria-expanded={menuOpen(s, { kind: "item", id: i.id })}
            aria-label={`More for ${name}`}
            onClick={e => toggleMenu({ kind: "item", id: i.id }, e.currentTarget)}
          >
            <Icon name="more" />
          </button>
        </div>
      </div>
      <Title i={i} />
      <Body i={i} s={s} />
      <Actions i={i} s={s} />
      {c && (
        <div className="from-conv">
          {"From "}
          <a
            href={linkTo({ conv: c.id })}
            onClick={e => {
              if (e.metaKey || e.ctrlKey || e.shiftKey) return;
              e.preventDefault();
              goConv(c.id);
            }}
          >
            {`${speakerName(s, c.speaker) || "Someone"}'s conversation, ${time(c.started)}`}
          </a>
        </div>
      )}
    </article>
  );
}

function Title({ i }: { i: Item }) {
  const url = i.kind === "link" ? safeUrl(i.url) : undefined;
  return (
    <h3 className="item-title">
      {url ? (
        <a href={url} target="_blank" rel="noopener noreferrer" onClick={() => followed(i)}>
          {i.title}
        </a>
      ) : (
        itemName(i)
      )}
    </h3>
  );
}

const shownEntries = (s: State, i: Item, compact?: boolean) => {
  const entries = i.entries || [];
  return s.lists.has(i.id) ? entries : entries.slice(0, compact ? 3 : 5);
};

function Body({ i, s, compact }: { i: Item; s: State; compact?: boolean }) {
  if (i.kind === "link")
    return (
      <>
        {i.site ? <div className="site">{i.site}</div> : null}
        {i.description ? <div className="desc">{i.description}</div> : null}
      </>
    );
  if (i.kind === "note") return <div className="prose clip" dangerouslySetInnerHTML={{ __html: md(i.body || "") }} />;
  if (i.kind === "list") {
    const entries = i.entries || [];
    const done = entries.filter(e => e.done).length;
    return (
      <>
        <div className="progress-line">{`${done} of ${entries.length} done`}</div>
        <div className="entries">
          {shownEntries(s, i, compact).map((e, n) => (
            <button
              key={n}
              className="entry"
              role="checkbox"
              aria-checked={e.done}
              onClick={() => tick(i.id, n, !e.done)}
            >
              <span className="box">{e.done ? <Icon name="check" size="s" /> : null}</span>
              <span className="txt">{e.text}</span>
            </button>
          ))}
        </div>
      </>
    );
  }
  const meta = [i.name, fmtSize(i.size)].filter(Boolean).join(", ");
  if (isImage(i) && (i.preview || i.url))
    return (
      <>
        <button className="thumb-btn" aria-label={`Show ${itemName(i)}`} onClick={() => showItem(i)}>
          <img className="preview" src={i.preview || i.url || undefined} alt={itemName(i)} loading="lazy" />
        </button>
        <div className="file-name">{meta}</div>
      </>
    );
  return (
    <div className="file">
      <span className="file-tile">
        <Icon name="file" />
        {fileType(i)}
      </span>
      <div className="file-name">
        {i.name}
        <br />
        {fmtSize(i.size)}
      </div>
    </div>
  );
}

function Actions({ i, s, compact }: { i: Item; s: State; compact?: boolean }) {
  const copy = (label: string) => (
    <button className="btn ghost" onClick={() => copyItem(i)}>
      <Icon name="copy" size="s" />
      {label}
    </button>
  );
  if (i.kind === "link") {
    const url = safeUrl(i.url);
    return (
      <div className="btns">
        <a className="btn" href={url} target="_blank" rel="noopener noreferrer" onClick={() => followed(i)}>
          Open
          <Icon name="open" size="s" />
        </a>
        {copy(COPY_LABEL.link)}
      </div>
    );
  }
  if (i.kind === "note")
    return (
      <div className="btns">
        <button className="btn" onClick={() => showItem(i)}>
          Open
        </button>
        {copy(COPY_LABEL.note)}
      </div>
    );
  if (i.kind === "list") {
    const rest = (i.entries || []).length - (compact ? 3 : 5);
    return (
      <div className="btns">
        {rest > 0 && (
          <button className="btn" aria-expanded={s.lists.has(i.id)} onClick={() => toggleWholeList(i.id)}>
            {s.lists.has(i.id) ? "Show fewer" : `${rest} more`}
          </button>
        )}
        {copy(COPY_LABEL.list)}
      </div>
    );
  }
  return (
    <div className="btns">
      <a className="btn" href={i.url ?? undefined} download={i.name || ""} onClick={() => followed(i)}>
        <Icon name="download" size="s" />
        Download
      </a>
    </div>
  );
}
