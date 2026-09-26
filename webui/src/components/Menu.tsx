import {
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";
import { createPortal } from "react-dom";
import {
  type MenuState,
  type State,
  cName,
  closeMenu,
  convById,
  copyItem,
  copyTranscript,
  delConv,
  delEvent,
  delItem,
  download,
  goConv,
  itemById,
  markSeen,
  merge,
  moveTo,
  newVoice,
  openLink,
  rename,
  setLabel,
  setTab,
  showNote,
  toggleNotPerson,
  useStore,
  wakeNotForTars,
} from "../store";
import type { Cluster, Conversation, Item, TarsEvent } from "../types";

/** A dropdown under its ⋯ button; on a phone, a bottom sheet over a scrim. */
export function Menu() {
  const s = useStore(),
    m = s.menu;
  if (!m) return null;
  const body = menuBody(s, m);
  return body ? <Popup m={m}>{body}</Popup> : null;
}

function menuBody(s: State, m: MenuState) {
  switch (m.kind) {
    case "voiceCard":
      return voiceMenu(
        s,
        s.clusters.find(c => c.id === m.id),
      );
    case "item":
      return itemMenu(s, itemById(s, m.id));
    case "conversation":
      return convMenu(s, convById(s, m.id));
    case "more":
      return moreMenu(s);
    case "event":
      return eventMenu(
        s,
        s.events.find(e => e.id === m.id),
        false,
      );
    case "eventVoice":
      return eventMenu(
        s,
        s.events.find(e => e.id === m.id),
        true,
      );
  }
}

const buttons = (el: HTMLElement) => [...el.querySelectorAll<HTMLButtonElement>("button")];

function Popup({ m, children }: { m: MenuState; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = ref.current!;
    if (!m.sheet) {
      const r = m.anchor.getBoundingClientRect(),
        h = el.offsetHeight,
        w = el.offsetWidth;
      const below = r.bottom + h + 8 < innerHeight;
      el.style.top = (below ? r.bottom + 4 : Math.max(8, r.top - h - 4)) + scrollY + "px";
      el.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + scrollX + "px";
    }
    buttons(el)[0]?.focus({ preventScroll: true });
  }, [m]);

  useEffect(() => {
    const outside = (e: MouseEvent) => {
      const t = e.target as Element;
      if (!t.closest(".menu") && !t.closest("[aria-haspopup=menu]")) closeMenu();
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeMenu();
        m.anchor.focus();
      }
    };
    // A phone's address bar showing or hiding changes only the height: that isn't a reason to close.
    let width = innerWidth;
    const resize = () => {
      if (innerWidth !== width) closeMenu();
      width = innerWidth;
    };
    document.addEventListener("click", outside);
    document.addEventListener("keydown", esc);
    addEventListener("resize", resize);
    return () => {
      document.removeEventListener("click", outside);
      document.removeEventListener("keydown", esc);
      removeEventListener("resize", resize);
    };
  }, [m]);

  // Up and down move between the menu's items; Tab out of it closes it.
  const keys = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const bs = buttons(ref.current!),
      i = bs.indexOf(document.activeElement as HTMLButtonElement);
    bs[(i + (e.key === "ArrowDown" ? 1 : bs.length - 1)) % bs.length]?.focus();
  };
  const blur = (e: ReactFocusEvent<HTMLDivElement>) => {
    if (e.relatedTarget && !ref.current!.contains(e.relatedTarget as Node)) closeMenu();
  };

  return createPortal(
    <>
      {m.sheet && <div className="scrim" onClick={closeMenu} />}
      <div ref={ref} className={`menu${m.sheet ? " sheet" : ""}`} role="menu" onKeyDown={keys} onBlur={blur}>
        {children}
      </div>
    </>,
    document.body,
  );
}

/** A menu item: closes the menu, then acts. */
function MenuItem({
  act,
  className,
  checked,
  children,
}: {
  act: () => void;
  className?: string;
  checked?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      className={className}
      role={checked === undefined ? "menuitem" : "menuitemradio"}
      aria-checked={checked}
      onClick={() => {
        closeMenu();
        act();
      }}
    >
      {children}
    </button>
  );
}

function voices(s: State, current: number | null, move: (cid: number) => void, fresh: () => void) {
  return (
    <>
      {s.clusters.map(c => (
        <MenuItem key={c.id} act={() => move(c.id)} checked={current === c.id}>
          {cName(s, c.id)}
          <span className="d">{c.size}</span>
        </MenuItem>
      ))}
      <MenuItem act={fresh}>New voice…</MenuItem>
    </>
  );
}

function eventMenu(s: State, e: TarsEvent | undefined, voiceOnly: boolean) {
  if (!e) return null;
  const vs = e.utterance_audio && (
    <>
      <h4>This request's voice</h4>
      {voices(
        s,
        e.cluster_id,
        cid => moveTo(e.id, cid),
        () => newVoice(e.id),
      )}
    </>
  );
  if (voiceOnly) return vs || null;
  return (
    <>
      {vs && (
        <>
          {vs}
          <hr />
        </>
      )}
      {e.label && <MenuItem act={() => setLabel(e.id, e.label!)}>Clear my label</MenuItem>}
      <MenuItem className="bad" act={() => delEvent(e.id)}>
        Delete event…
      </MenuItem>
    </>
  );
}

function voiceMenu(s: State, c: Cluster | undefined) {
  if (!c) return null;
  const others = s.clusters.filter(x => x.id !== c.id);
  return (
    <>
      <MenuItem act={() => rename(c.id)}>{c.name ? "Rename…" : "Name…"}</MenuItem>
      <MenuItem act={() => toggleNotPerson(c.id)}>
        {c.kind === "not_person" ? "It's a person" : "Not a person (TV, radio…)"}
      </MenuItem>
      {others.length > 0 && (
        <>
          <hr />
          <h4>Same person as…</h4>
          {others.map(o => (
            <MenuItem key={o.id} act={() => merge(c.id, o.id)}>
              {cName(s, o.id)}
              <span className="d">{o.size}</span>
            </MenuItem>
          ))}
        </>
      )}
    </>
  );
}

function itemMenu(s: State, i: Item | undefined) {
  if (!i) return null;
  return (
    <>
      {i.kind === "link" && <MenuItem act={() => openLink(i)}>Open link ↗</MenuItem>}
      {i.kind === "note" && <MenuItem act={() => showNote(i)}>Open</MenuItem>}
      {i.kind === "file" && <MenuItem act={() => download(i)}>Download</MenuItem>}
      {i.kind !== "file" && (
        <MenuItem act={() => copyItem(i)}>{i.kind === "link" ? "Copy link" : "Copy text"}</MenuItem>
      )}
      {!i.seen && <MenuItem act={() => markSeen(i)}>Mark seen</MenuItem>}
      {convById(s, i.conversation_id) && (
        <MenuItem act={() => goConv(i.conversation_id)}>Show the conversation</MenuItem>
      )}
      <hr />
      <MenuItem className="bad" act={() => delItem(i.id)}>
        Delete…
      </MenuItem>
    </>
  );
}

function convMenu(s: State, c: Conversation | undefined) {
  if (!c) return null;
  const wake = s.events.find(e => e.id === c.wake?.event_id);
  return (
    <>
      <MenuItem act={() => copyTranscript(c)}>Copy transcript</MenuItem>
      {wake && (
        <MenuItem act={() => wakeNotForTars(c)}>
          {wake.label === "not_real" ? "It was for TARS" : "Not meant for TARS"}
        </MenuItem>
      )}
      {wake?.utterance_audio && (
        <>
          <hr />
          <h4>Who was talking</h4>
          {voices(
            s,
            wake.cluster_id,
            cid => moveTo(wake.id, cid),
            () => newVoice(wake.id),
          )}
        </>
      )}
      <hr />
      <MenuItem className="bad" act={() => delConv(c.id)}>
        Delete conversation…
      </MenuItem>
    </>
  );
}

function moreMenu(s: State) {
  return (
    <>
      <MenuItem act={() => setTab("voices")} checked={s.tab === "voices"}>
        Voices
      </MenuItem>
      <MenuItem act={() => setTab("models")} checked={s.tab === "models"}>
        Models
      </MenuItem>
    </>
  );
}
