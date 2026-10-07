/* Opens the state a demo link names (see demoParams.ts). Loaded with ?demo only. */
import { showPlaying } from "./audio";
import { MOCK_TOASTS } from "./demo";
import { DEMO_LINK } from "./demoParams";
import { LINK } from "./params";
import {
  type MenuTarget,
  answerWake,
  cancelReminder,
  closeToast,
  delConv,
  delEvent,
  delItem,
  findTurn,
  get,
  itemById,
  newVoice,
  openMerge,
  refresh,
  rename,
  reviewEvents,
  rollback,
  scrollToEl,
  set,
  setNow,
  showItem,
  toast,
  toggleMenu,
} from "./store";

/** States that show before the data loads. False when the state replaces the data entirely. */
export function openEarlyState() {
  if (DEMO_LINK.state === "error") {
    set({ phase: "error", error: "No answer from the server (timed out after 10 s)" });
    return false;
  }
  if (DEMO_LINK.state === "loading") return false;
  set({ reclustering: DEMO_LINK.reclustering });
  return true;
}

const openConv = (id: number) => setNow(s => ({ open: new Set([...s.open, id]) }));

export async function openLinkState() {
  await document.fonts.ready;
  if (DEMO_LINK.answered != null) {
    await answerWake(DEMO_LINK.answered, DEMO_LINK.answer);
    closeToast();
    if (DEMO_LINK.refreshed) await refresh();
    else scrollToEl(`.wake[data-id="${DEMO_LINK.answered}"]`);
  }
  if (DEMO_LINK.edit != null) {
    const [, c] = findTurn(get(), DEMO_LINK.edit);
    if (c) {
      openConv(c.id);
      setNow({ edit: DEMO_LINK.edit });
      scrollToEl(`.conv[data-id="${c.id}"]`);
    }
  }
  if (DEMO_LINK.playing) {
    const turn = DEMO_LINK.playing.match(/^turn-(\d+)$/);
    const [, c] = turn ? findTurn(get(), Number(turn[1])) : [];
    if (c && LINK.conv == null) {
      openConv(c.id);
      scrollToEl(`.conv[data-id="${c.id}"]`);
    }
    showPlaying(DEMO_LINK.playing, 0.35, 3);
  }
  if (DEMO_LINK.toast) toast(MOCK_TOASTS[DEMO_LINK.toast], undefined, 0);
  if (DEMO_LINK.more) {
    const button = document.querySelector<HTMLElement>(".more-btn");
    if (button) toggleMenu({ kind: "more" }, button);
  }
  if (DEMO_LINK.menu && DEMO_LINK.menuId != null) linkedMenu(DEMO_LINK.menu, DEMO_LINK.menuId);
  openModal();
  await new Promise(r => setTimeout(r, 0));
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
}

function openMenu(target: MenuTarget, button: string, around?: string) {
  if (around) scrollToEl(around);
  const el = document.querySelector<HTMLElement>(button);
  if (el && getComputedStyle(el).display !== "none") toggleMenu(target, el);
}

function linkedMenu(kind: NonNullable<typeof DEMO_LINK.menu>, id: number) {
  const wake = `.wake[data-id="${id}"]`;
  switch (kind) {
    case "item":
      return openMenu({ kind: "item", id }, `.item[data-id="${id}"] [aria-haspopup=menu]`, `.item[data-id="${id}"]`);
    case "wake":
      return openMenu({ kind: "event", id }, `${wake} .wake-head [aria-haspopup=menu]`, wake);
    case "reply":
      return openMenu({ kind: "eventVoice", id }, `${wake} .chip`, wake);
    case "voice":
      return openMenu({ kind: "voice", id }, `.voice[data-id="${id}"] [aria-haspopup=menu]`, `.voice[data-id="${id}"]`);
    case "who":
      openConv(id);
      return openMenu(
        { kind: "who", id },
        `.conv[data-id="${id}"] .p-actions [aria-haspopup=menu]`,
        `.conv[data-id="${id}"]`,
      );
  }
}

function openModal() {
  const s = get();
  const id = DEMO_LINK.modalId;
  switch (DEMO_LINK.modal) {
    case "delete-conv": {
      const convId = id ?? s.convs[0]?.id;
      if (convId != null) delConv(convId);
      break;
    }
    case "delete-item": {
      const itemId = id ?? s.items[0]?.id;
      if (itemId != null) delItem(itemId);
      break;
    }
    case "note":
      showItem((id != null ? itemById(s, id) : undefined) ?? s.items.find(i => i.kind === "note"));
      break;
    case "delete-wake": {
      const eventId = id ?? reviewEvents(s)[0]?.id;
      if (eventId != null) delEvent(eventId);
      break;
    }
    case "newvoice": {
      if (s.tab === "review") {
        const event = id ?? s.events.find(e => e.has_request_audio)?.id;
        if (event != null) newVoice(event);
      } else {
        const cluster = id ?? s.clusters.find(c => c.kind === "unknown")?.id;
        if (cluster != null) rename(cluster);
      }
      break;
    }
    case "merge": {
      const cluster = id ?? s.clusters.find(c => c.kind === "unknown")?.id;
      if (cluster != null) openMerge(cluster);
      break;
    }
    case "rollback": {
      const version = (id == null && DEMO_LINK.modalArg) || s.models?.history.find(h => !h.active)?.version;
      if (version) rollback(version);
      break;
    }
    case "stop": {
      const all = s.reminders?.reminders ?? [];
      const r = all.find(x => x.id === id) ?? all.find(x => x.status === "scheduled");
      if (r) cancelReminder(r);
      break;
    }
  }
}
