/* Opens the state a link names (see params.ts), for the screenshot tests and sharing a view. */
import { showPlaying } from "./audio";
import { LINK } from "./params";
import {
  delConv,
  delEvent,
  delItem,
  get,
  itemById,
  merge,
  newVoice,
  reviewEvents,
  rollback,
  scrollToEl,
  set,
  setNow,
  showNote,
  toast,
  toggleMenu,
} from "./store";

export async function openLinkState() {
  if (LINK.retrain && LINK.retrain !== "running") {
    const { MOCK_RETRAIN } = await import("./demo");
    setNow({ lastRetrain: { ...MOCK_RETRAIN[LINK.retrain], ts: Date.now() / 1000 - 60 } });
  }
  if (LINK.playing) showPlaying(LINK.playing, 0.35);
  if (LINK.conv != null || LINK.item != null) await document.fonts.ready; // the text above has its final height
  if (LINK.conv != null) scrollToEl(`.conv[data-id="${LINK.conv}"]`);
  if (LINK.item != null) scrollToEl(`.item[data-id="${LINK.item}"]`);
  if (LINK.toast) {
    const { MOCK_TOASTS } = await import("./demo");
    toast(MOCK_TOASTS[LINK.toast], undefined, 0);
  }
  if (LINK.more) {
    const b = document.querySelector<HTMLElement>(".moreb");
    if (b && getComputedStyle(b).display !== "none") toggleMenu({ kind: "more" }, b);
  }
  openModal();
}

function openModal() {
  const s = get(),
    id = LINK.modalId;
  switch (LINK.modal) {
    case "delete-conv": {
      const c = id ?? s.convs[0]?.id;
      if (c != null) delConv(c);
      break;
    }
    case "delete-item": {
      const i = id ?? s.items[0]?.id;
      if (i != null) delItem(i);
      break;
    }
    case "note":
      showNote((id != null ? itemById(s, id) : undefined) ?? s.items.find(i => i.kind === "note"));
      break;
    case "delete": {
      const e = id ?? reviewEvents(s)[0]?.id;
      if (e != null) delEvent(e);
      break;
    }
    case "newvoice": {
      const e = s.events.find(e => e.utterance_audio);
      if (e) newVoice(e.id);
      break;
    }
    case "merge": {
      const last = s.clusters[s.clusters.length - 1],
        other = s.clusters.find(o => o.id !== last?.id);
      if (last && other) merge(last.id, other.id);
      break;
    }
    case "rollback": {
      const v = s.models?.history?.find(h => !h.active)?.version;
      if (v) rollback(v);
      break;
    }
  }
}

/** A state that shows before (or instead of) the data. False when there's no data to load. */
export function openEarlyState() {
  if (LINK.state === "error") {
    set({ phase: "error", error: "Failed to fetch" });
    return false;
  }
  if (LINK.state === "loading") return false;
  if (LINK.reclustering) set({ reclustering: true });
  if (LINK.retrain === "running") set({ retraining: true });
  return true;
}
