import {
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";
import { createPortal } from "react-dom";
import { COPY_LABEL, ITEM_KIND_LABEL } from "../format";
import {
  type MenuState,
  type State,
  answerWake,
  closeMenu,
  clusterById,
  clusterName,
  convById,
  copyItem,
  copyTranscript,
  delConv,
  delEvent,
  delItem,
  download,
  eventById,
  goConv,
  itemById,
  markSeen,
  merge,
  moveTo,
  newVoice,
  openLink,
  rename,
  setTab,
  showItem,
  toggleNotPerson,
  toggleWakeNotForTars,
  useStore,
} from "../store";
import type { Cluster, Conversation, Item, TarsEvent } from "../types";

export function Menu() {
  const s = useStore();
  const m = s.menu;
  if (!m) return null;
  const body = menuBody(s, m);
  return body ? <Popup m={m}>{body}</Popup> : null;
}

function menuBody(s: State, m: MenuState) {
  switch (m.kind) {
    case "voice":
      return voiceMenu(s, clusterById(s, m.id));
    case "item":
      return itemMenu(s, itemById(s, m.id));
    case "conversation":
      return convMenu(s, convById(s, m.id));
    case "more":
      return moreMenu(s);
    case "event":
      return eventMenu(s, eventById(s, m.id), false);
    case "eventVoice":
      return eventMenu(s, eventById(s, m.id), true);
  }
}

const buttons = (el: HTMLElement) => [...el.querySelectorAll<HTMLButtonElement>("button")];

function Popup({ m, children }: { m: MenuState; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!m.sheet) {
      const r = m.anchor.getBoundingClientRect();
      const height = el.offsetHeight;
      const width = el.offsetWidth;
      const below = r.bottom + height + 8 < innerHeight;
      el.style.top = (below ? r.bottom + 4 : Math.max(8, r.top - height - 4)) + scrollY + "px";
      el.style.left = Math.max(8, Math.min(r.right - width, innerWidth - width - 8)) + scrollX + "px";
    }
    buttons(el)[0]?.focus({ preventScroll: true });
  }, [m]);

  useEffect(() => {
    const outside = (e: MouseEvent) => {
      const t = e.target as Element;
      if (!t.closest(".menu") && !t.closest("[aria-haspopup=menu]")) closeMenu();
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeMenu(true);
    };
    // A phone's address bar showing or hiding changes only the height: that isn't a reason to close.
    let width = innerWidth;
    const resize = () => {
      if (innerWidth !== width) closeMenu();
      width = innerWidth;
    };
    document.addEventListener("click", outside);
    document.addEventListener("keydown", escape);
    addEventListener("resize", resize);
    return () => {
      document.removeEventListener("click", outside);
      document.removeEventListener("keydown", escape);
      removeEventListener("resize", resize);
    };
  }, []);

  const keys = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if ((e.key !== "ArrowDown" && e.key !== "ArrowUp") || !ref.current) return;
    e.preventDefault();
    const all = buttons(ref.current);
    const at = all.indexOf(document.activeElement as HTMLButtonElement);
    all[(at + (e.key === "ArrowDown" ? 1 : all.length - 1)) % all.length]?.focus();
  };
  const blur = (e: ReactFocusEvent<HTMLDivElement>) => {
    if (e.relatedTarget && !ref.current?.contains(e.relatedTarget as Node)) closeMenu();
  };

  return createPortal(
    <>
      {m.sheet && <div className="scrim" onClick={() => closeMenu()} />}
      <div ref={ref} className={`menu${m.sheet ? " sheet" : ""}`} role="menu" onKeyDown={keys} onBlur={blur}>
        {children}
      </div>
    </>,
    document.body,
  );
}

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
        closeMenu(true);
        act();
      }}
    >
      {children}
    </button>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={title}>
      <h4 aria-hidden="true">{title}</h4>
      {children}
    </div>
  );
}

function voices(s: State, title: string, current: number | null, move: (clusterId: number) => void, fresh: () => void) {
  return (
    <Section title={title}>
      {s.clusters.map(c => (
        <MenuItem key={c.id} act={() => move(c.id)} checked={current === c.id}>
          {clusterName(s, c.id)}
          <span className="menu-count">{c.size}</span>
        </MenuItem>
      ))}
      <MenuItem act={fresh}>New voice…</MenuItem>
    </Section>
  );
}

function eventMenu(s: State, e: TarsEvent | undefined, voiceOnly: boolean) {
  if (!e) return null;
  const label = e.label;
  const voice = e.has_request_audio
    ? voices(
        s,
        "This request's voice",
        e.cluster_id,
        id => moveTo(e.id, id),
        () => newVoice(e.id),
      )
    : null;
  if (voiceOnly) return voice;
  return (
    <>
      {voice && (
        <>
          {voice}
          <hr />
        </>
      )}
      {label && <MenuItem act={() => answerWake(e.id, label)}>Clear my answer</MenuItem>}
      <MenuItem className="bad" act={() => delEvent(e.id)}>
        Delete wake…
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
          <Section title="Same person as…">
            {others.map(o => (
              <MenuItem key={o.id} act={() => merge(c.id, o.id)}>
                {clusterName(s, o.id)}
                <span className="menu-count">{o.size}</span>
              </MenuItem>
            ))}
          </Section>
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
      {i.kind === "note" && <MenuItem act={() => showItem(i)}>Open</MenuItem>}
      {i.kind === "file" && <MenuItem act={() => download(i)}>Download</MenuItem>}
      {i.kind !== "file" && <MenuItem act={() => copyItem(i)}>{COPY_LABEL[i.kind]}</MenuItem>}
      {!i.seen && <MenuItem act={() => markSeen(i)}>Mark seen</MenuItem>}
      {convById(s, i.conversation_id) && (
        <MenuItem act={() => goConv(i.conversation_id)}>Show the conversation</MenuItem>
      )}
      <hr />
      <MenuItem className="bad" act={() => delItem(i.id)}>
        Delete {ITEM_KIND_LABEL[i.kind].toLowerCase()}…
      </MenuItem>
    </>
  );
}

function convMenu(s: State, c: Conversation | undefined) {
  if (!c) return null;
  const wake = c.wake;
  return (
    <>
      <MenuItem act={() => copyTranscript(c)}>Copy transcript</MenuItem>
      {wake && (
        <MenuItem act={() => toggleWakeNotForTars(c)}>
          {wake.label === "not_real" ? "It was for TARS" : "Not meant for TARS"}
        </MenuItem>
      )}
      {wake?.has_request_audio && (
        <>
          <hr />
          {voices(
            s,
            "Who was talking",
            wake.cluster_id,
            id => moveTo(wake.event_id, id),
            () => newVoice(wake.event_id),
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
