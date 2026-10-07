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
  answerWake,
  closeMenu,
  clusterById,
  clusterName,
  convById,
  copyItem,
  delEvent,
  delItem,
  eventById,
  goConv,
  itemById,
  markSeen,
  moveTo,
  newVoice,
  openMerge,
  rename,
  setTab,
  toggleNotPerson,
  useStore,
} from "../store";
import type { Cluster, Item, TarsEvent } from "../types";
import { Icon, type IconName } from "./Icon";

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
    case "who": {
      const wake = convById(s, m.id)?.wake;
      return wake ? voicePicker(s, wake.event_id, wake.cluster_id) : null;
    }
    case "more":
      return moreMenu(s);
    case "event":
      return eventMenu(eventById(s, m.id));
    case "eventVoice": {
      const e = eventById(s, m.id);
      return e ? voicePicker(s, e.id, e.cluster_id) : null;
    }
  }
}

const buttons = (el: HTMLElement) => [...el.querySelectorAll<HTMLButtonElement>("button")];

function Popup({ m, children }: { m: MenuState; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const root = document.getElementById("root") ?? document.body;
  const host = m.anchor.closest<HTMLElement>(".anchor") ?? root;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (m.anchor.matches(":focus-visible")) buttons(el)[0]?.focus({ preventScroll: true });
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

  return (
    <>
      {createPortal(<div className="scrim clear" onClick={() => closeMenu()} />, root)}
      {createPortal(
        <div ref={ref} className={host === root ? "menu more-menu" : "menu"} role="menu" onKeyDown={keys} onBlur={blur}>
          <div className="grabber" />
          {children}
        </div>,
        host,
      )}
    </>
  );
}

function MenuItem({
  act,
  icon,
  checked,
  children,
}: {
  act: () => void;
  icon: IconName;
  checked?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      role={checked === undefined ? "menuitem" : "menuitemradio"}
      aria-checked={checked}
      onClick={() => {
        closeMenu(true);
        act();
      }}
    >
      <Icon name={icon} />
      {children}
    </button>
  );
}

function voicePicker(s: State, eventId: number, current: number | null) {
  return (
    <>
      <div className="menu-title">Whose voice is it?</div>
      {s.clusters
        .filter(c => c.kind !== "not_person" || c.id === current)
        .map(c => (
          <MenuItem
            key={c.id}
            icon={c.id === current ? "check" : "person"}
            checked={c.id === current}
            act={() => moveTo(eventId, c.id)}
          >
            {clusterName(s, c.id)}
          </MenuItem>
        ))}
      <hr />
      <MenuItem icon="edit" act={() => newVoice(eventId)}>
        Someone new…
      </MenuItem>
    </>
  );
}

function eventMenu(e: TarsEvent | undefined) {
  if (!e) return null;
  const label = e.label;
  return (
    <>
      {label && (
        <MenuItem icon="close" act={() => answerWake(e.id, label)}>
          Clear my answer
        </MenuItem>
      )}
      <MenuItem icon="trash" act={() => delEvent(e.id)}>
        Delete this wake
      </MenuItem>
    </>
  );
}

function voiceMenu(s: State, c: Cluster | undefined) {
  if (!c) return null;
  const others = s.clusters.filter(x => x.id !== c.id && x.kind !== "not_person");
  return (
    <>
      <MenuItem icon="edit" act={() => rename(c.id)}>
        {c.name ? "Rename" : "Name it"}
      </MenuItem>
      {others.length > 0 && (
        <MenuItem icon="merge" act={() => openMerge(c.id)}>
          Merge with another voice…
        </MenuItem>
      )}
      <MenuItem icon="notfor" act={() => toggleNotPerson(c.id)}>
        {c.kind === "not_person" ? "It's a person after all" : "Not a person"}
      </MenuItem>
    </>
  );
}

const COPY = { link: "Copy link", note: "Copy text", list: "Copy list", file: "Copy the file's address" };

function itemMenu(s: State, i: Item | undefined) {
  if (!i) return null;
  return (
    <>
      <MenuItem icon="copy" act={() => copyItem(i)}>
        {COPY[i.kind]}
      </MenuItem>
      {!i.seen && (
        <MenuItem icon="check" act={() => markSeen(i)}>
          Mark seen
        </MenuItem>
      )}
      {convById(s, i.conversation_id) && (
        <MenuItem icon="message" act={() => goConv(i.conversation_id)}>
          The conversation
        </MenuItem>
      )}
      <hr />
      <MenuItem icon="trash" act={() => delItem(i.id)}>
        Delete
      </MenuItem>
    </>
  );
}

function moreMenu(s: State) {
  return (
    <>
      <MenuItem icon="voices" act={() => setTab("voices")} checked={s.tab === "voices"}>
        Voices
      </MenuItem>
      <MenuItem icon="chip" act={() => setTab("models")} checked={s.tab === "models"}>
        Models
      </MenuItem>
    </>
  );
}
