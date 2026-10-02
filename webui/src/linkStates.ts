/* Opens the state a demo link names (see demoParams.ts). Loaded with ?demo only. */
import { showPlaying } from "./audio";
import { MOCK_TOASTS } from "./demo";
import { DEMO_LINK } from "./demoParams";
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
  set,
  showItem,
  toast,
  toggleMenu,
} from "./store";

/** States that show before the data loads. False when the state replaces the data entirely. */
export function openEarlyState() {
  if (DEMO_LINK.state === "error") {
    set({ phase: "error", error: "Failed to fetch" });
    return false;
  }
  if (DEMO_LINK.state === "loading") return false;
  set({ reclustering: DEMO_LINK.reclustering, edit: DEMO_LINK.edit });
  return true;
}

export function openLinkState() {
  if (DEMO_LINK.playing) showPlaying(DEMO_LINK.playing, 0.35);
  if (DEMO_LINK.toast) toast(MOCK_TOASTS[DEMO_LINK.toast], undefined, 0);
  if (DEMO_LINK.more) {
    const button = document.querySelector<HTMLElement>(".more-tab");
    if (button && getComputedStyle(button).display !== "none") toggleMenu({ kind: "more" }, button);
  }
  openModal();
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
      const event = s.events.find(e => e.has_request_audio);
      if (event) newVoice(event.id);
      break;
    }
    case "merge": {
      const last = s.clusters[s.clusters.length - 1];
      const other = s.clusters.find(c => c.id !== last?.id);
      if (last && other) merge(last.id, other.id);
      break;
    }
    case "rollback": {
      const version = s.models?.history.find(h => !h.active)?.version;
      if (version) rollback(version);
      break;
    }
  }
}
