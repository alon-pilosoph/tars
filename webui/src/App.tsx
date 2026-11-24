import { type MouseEvent, useEffect } from "react";
import { Dialog } from "./components/Dialog";
import { Home, Sent } from "./components/Home";
import { Icon, type IconName } from "./components/Icon";
import { Menu } from "./components/Menu";
import { Models } from "./components/Models";
import { Reminders } from "./components/Reminders";
import { Review } from "./components/Review";
import { Memory } from "./components/Memory";
import { Skills } from "./components/Skills";
import { Talk } from "./components/Talk";
import { LoadErrors, Loading, Unreachable } from "./components/States";
import { StatusView } from "./components/Status";
import { Toasts } from "./components/Toasts";
import { Voices } from "./components/Voices";
import { HEARING, type Tab, linkTo, tabIn } from "./params";
import {
  type State,
  menuOpen,
  refreshNow,
  remindersWaiting,
  reviewTodo,
  setTab,
  toggleMenu,
  unseen,
  useStore,
} from "./store";

const VIEWS = {
  home: Home,
  sent: Sent,
  review: Review,
  reminders: Reminders,
  skills: Skills,
  memory: Memory,
  voices: Voices,
  models: Models,
  status: StatusView,
  talk: Talk,
};

const COUNTED: Partial<Record<Tab, string>> = { review: "to review", reminders: "waiting for got it" };

const go = (tab: Tab) => (e: MouseEvent) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  setTab(tab);
};
const href = (tab: Tab) => linkTo(tab === "home" ? {} : { tab });

function Count({ tab, n }: { tab: Tab; n: number }) {
  if (!n) return null;
  return (
    <>
      {" "}
      <span className="count" aria-label={`${n} ${COUNTED[tab] ?? "new"}`}>
        {n}
      </span>
    </>
  );
}

function NavLink({ s, tab, label, n = 0 }: { s: State; tab: Tab; label: string; n?: number }) {
  return (
    <a href={href(tab)} aria-current={s.tab === tab ? "page" : undefined} onClick={go(tab)}>
      {label}
      <Count tab={tab} n={n} />
    </a>
  );
}

function BarLink({ s, tab, label, icon, n = 0 }: { s: State; tab: Tab; label: string; icon: IconName; n?: number }) {
  return (
    <a href={href(tab)} aria-current={s.tab === tab ? "page" : undefined} onClick={go(tab)}>
      <Icon name={icon} />
      <span>
        {label}
        <Count tab={tab} n={n} />
      </span>
    </a>
  );
}

export function App() {
  const s = useStore();
  const ready = s.phase === "ready";
  const counts = {
    sent: ready ? unseen(s).length : 0,
    review: ready ? reviewTodo(s).length : 0,
    reminders: ready ? remindersWaiting(s).length : 0,
  };
  useEffect(() => {
    document.title = counts.sent ? `TARS (${counts.sent})` : "TARS";
  }, [counts.sent]);
  // The address follows the page shown, so a refresh stays on it and Back goes to the one before.
  useEffect(() => {
    if (tabIn(location.search) !== s.tab) history.pushState(null, "", href(s.tab));
  }, [s.tab]);
  useEffect(() => {
    const back = () => setTab(tabIn(location.search));
    addEventListener("popstate", back);
    return () => removeEventListener("popstate", back);
  }, []);
  const aloud = s.status.aloud;
  const talk = Boolean(s.status.talk);
  const View = VIEWS[(!aloud && HEARING.includes(s.tab)) || (!talk && s.tab === "talk") ? "home" : s.tab];
  const loading = s.phase === "loading";
  const busy = s.refreshing || loading;
  return (
    <>
      <header className="top">
        <a className="wordmark" href={href("home")} onClick={go("home")}>
          TARS
        </a>
        {/* Until the first load says what's set up, no pages: they'd change as it lands. */}
        {!loading && (
          <nav className="nav" aria-label="Pages">
            <NavLink s={s} tab="home" label="Home" />
            <NavLink s={s} tab="sent" label="Sent" n={counts.sent} />
            {aloud && <NavLink s={s} tab="review" label="Review" n={counts.review} />}
            <NavLink s={s} tab="reminders" label="Reminders" n={counts.reminders} />
            {talk && <NavLink s={s} tab="talk" label="Talk" />}
          </nav>
        )}
        {!loading && (
          <div className="top-end">
            <nav className="nav-2" aria-label="More pages">
              <NavLink s={s} tab="skills" label="Skills" />
              <NavLink s={s} tab="memory" label="Memory" />
              {aloud && <NavLink s={s} tab="voices" label="Voices" />}
              {aloud && <NavLink s={s} tab="models" label="Models" />}
              {s.status.health && <NavLink s={s} tab="status" label="Status" />}
            </nav>
            <button className="refresh" onClick={() => busy || refreshNow()} aria-busy={s.refreshing}>
              <Icon name="refresh" size="s" />
              Refresh
            </button>
            <button className="icon-btn refresh-icon" aria-label="Refresh" onClick={() => busy || refreshNow()}>
              <Icon name="refresh" />
            </button>
            <button
              className="icon-btn more-btn"
              aria-label="More"
              aria-haspopup="menu"
              aria-expanded={menuOpen(s, { kind: "more" })}
              onClick={e => toggleMenu({ kind: "more" }, e.currentTarget)}
            >
              <Icon name="more" />
            </button>
          </div>
        )}
      </header>
      <main>
        {loading ? (
          <Loading />
        ) : s.phase === "error" ? (
          <Unreachable />
        ) : (
          <>
            <LoadErrors />
            <View />
          </>
        )}
      </main>
      {!loading && (
        <nav className="tabbar" aria-label="Pages">
          <BarLink s={s} tab="home" label="Home" icon="home" />
          <BarLink s={s} tab="sent" label="Sent" icon="sent" n={counts.sent} />
          {aloud && <BarLink s={s} tab="review" label="Review" icon="wave" n={counts.review} />}
          <BarLink s={s} tab="reminders" label="Reminders" icon="bell" n={counts.reminders} />
          {talk && <BarLink s={s} tab="talk" label="Talk" icon="mic" />}
        </nav>
      )}
      <Toasts />
      <Dialog />
      <Menu />
    </>
  );
}
