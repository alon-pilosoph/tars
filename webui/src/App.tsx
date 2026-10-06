import { useEffect } from "react";
import { Dialog } from "./components/Dialog";
import { Home, Sent } from "./components/Home";
import { Menu } from "./components/Menu";
import { Models } from "./components/Models";
import { Reminders } from "./components/Reminders";
import { Review } from "./components/Review";
import { Loading, Unreachable } from "./components/States";
import { Toasts } from "./components/Toasts";
import { Voices } from "./components/Voices";
import type { Tab } from "./params";
import { type State, refreshNow, reviewTodo, setTab, toggleMenu, unseen, useStore } from "./store";

const VIEWS = { home: Home, sent: Sent, review: Review, reminders: Reminders, voices: Voices, models: Models };

const COUNTED: Partial<Record<Tab, string>> = { review: "to review", reminders: "waiting for got it" };

function TabButton({
  s,
  tab,
  label,
  count = 0,
  secondary,
}: {
  s: State;
  tab: Tab;
  label: string;
  count?: number;
  secondary?: boolean;
}) {
  return (
    <button
      className={secondary ? "secondary" : ""}
      aria-current={s.tab === tab ? "page" : undefined}
      onClick={() => setTab(tab)}
    >
      {label}
      {count ? (
        <span className="count" aria-label={`${count} ${COUNTED[tab] ?? "new"}`}>
          {count}
        </span>
      ) : null}
    </button>
  );
}

export function App() {
  const s = useStore();
  const ready = s.phase === "ready";
  const unseenCount = ready ? unseen(s).length : 0;
  const toReviewCount = ready ? reviewTodo(s).length : 0;
  const waitingCount = ready ? (s.reminders?.reminders.filter(r => r.status === "waiting").length ?? 0) : 0;
  useEffect(() => {
    document.title = unseenCount ? `TARS (${unseenCount})` : "TARS";
  }, [unseenCount]);
  const View = VIEWS[s.tab];
  return (
    <>
      <header className="top">
        <div className="top-in">
          <div className="mark" aria-label="TARS">
            <i />
            <i />
            TARS
          </div>
          <nav className="nav" aria-label="Sections">
            <TabButton s={s} tab="home" label="Home" />
            <TabButton s={s} tab="sent" label="Sent" count={unseenCount} />
            <TabButton s={s} tab="review" label="Review" count={toReviewCount} />
            <TabButton s={s} tab="reminders" label="Reminders" count={waitingCount} />
            <span className="nav-gap" />
            <TabButton s={s} tab="voices" label="Voices" secondary />
            <TabButton s={s} tab="models" label="Models" secondary />
            <button
              className="more-tab"
              aria-haspopup="menu"
              aria-current={s.tab === "voices" || s.tab === "models" ? "page" : undefined}
              onClick={e => toggleMenu({ kind: "more" }, e.currentTarget)}
            >
              More
            </button>
          </nav>
          <button
            className="refresh"
            onClick={refreshNow}
            disabled={s.refreshing || s.phase === "loading"}
            aria-busy={s.refreshing}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" />
              <path d="M12.4 1.6v3h-3" />
            </svg>
            Refresh
          </button>
        </div>
      </header>
      <main>{s.phase === "loading" ? <Loading /> : s.phase === "error" ? <Unreachable /> : <View />}</main>
      <Toasts />
      <Dialog />
      <Menu />
    </>
  );
}
