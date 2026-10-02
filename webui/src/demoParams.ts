/* The demo's own link parameters (with ?demo only): its data, and states for the screenshot tests.

   ?demo=empty: no data     ?demo=long: everything long
   ?seen=all: everything seen     ?review=done: every wake answered
   ?state=loading|error     ?edit=<turn id>: correcting it     ?more=1: the phone's More menu open
   ?playing=<event id>-wake|<event id>-request|turn-<turn id>: a clip shown as playing
   ?modal=delete-conv[:id]|delete-item[:id]|note[:id]|delete-wake[:id]|newvoice|merge|rollback
   ?toast=recluster|renamed     ?recluster=running */
import { PARAMS, idParam, oneOf } from "./params";

const MODALS = ["delete-conv", "delete-item", "note", "delete-wake", "newvoice", "merge", "rollback"] as const;
const [modal, modalId] = (PARAMS.get("modal") || "").split(":");

export const DEMO_LINK = {
  data: PARAMS.get("demo") || "",
  state: oneOf(PARAMS.get("state"), ["loading", "error"] as const),
  seenAll: PARAMS.get("seen") === "all",
  reviewDone: PARAMS.get("review") === "done",
  edit: idParam(PARAMS.get("edit")),
  more: PARAMS.get("more") === "1",
  playing: PARAMS.get("playing"),
  modal: oneOf(modal, MODALS),
  modalId: idParam(modalId),
  toast: oneOf(PARAMS.get("toast"), ["recluster", "renamed"] as const),
  reclustering: PARAMS.get("recluster") === "running",
};
