/* The demo's own link parameters (with ?demo only): its data, and states for the screenshot tests.

   ?demo=empty: no data     ?demo=long: everything long     ?reminders=off: reminders switched off
   ?seen=all: everything seen     ?review=done: every wake answered
   ?state=loading|error     ?edit=<turn id>: correcting it     ?more=1: the phone's More menu open
   ?playing=<event id>-wake|<event id>-request|turn-<turn id>: a clip shown as playing
   ?modal=delete-conv[:id]|delete-item[:id]|note[:id]|delete-wake[:id]|newvoice[:id]|merge[:id]|rollback[:version]
          |stop[:reminder id]
   ?menu=item:<id>|wake:<id>|reply:<event id>|voice:<id>|who:<conversation id>: a menu open
   ?answered=<event id>:real|not_real: a wake answered on this page, before Refresh; with &refreshed=1, after it
   ?form=minutes|time|back|error: the New reminder form, filled in
   ?toast=recluster|renamed     ?recluster=running */
import { PARAMS, idParam, oneOf } from "./params";

const MODALS = ["delete-conv", "delete-item", "note", "delete-wake", "newvoice", "merge", "rollback", "stop"] as const;
const MENUS = ["item", "wake", "reply", "voice", "who"] as const;
const [modal, modalArg] = (PARAMS.get("modal") || "").split(":");
const [menu, menuId] = (PARAMS.get("menu") || "").split(":");
const [answered, answer] = (PARAMS.get("answered") || "").split(":");

export const DEMO_LINK = {
  data: PARAMS.get("demo") || "",
  remindersOff: PARAMS.get("reminders") === "off",
  state: oneOf(PARAMS.get("state"), ["loading", "error"] as const),
  seenAll: PARAMS.get("seen") === "all",
  reviewDone: PARAMS.get("review") === "done",
  edit: idParam(PARAMS.get("edit")),
  more: PARAMS.get("more") === "1",
  playing: PARAMS.get("playing"),
  modal: oneOf(modal, MODALS),
  modalId: idParam(modalArg),
  modalArg: modalArg || null,
  menu: oneOf(menu, MENUS),
  menuId: idParam(menuId),
  answered: idParam(answered),
  answer: oneOf(answer, ["real", "not_real"] as const) ?? "real",
  refreshed: PARAMS.get("refreshed") === "1",
  form: oneOf(PARAMS.get("form"), ["minutes", "time", "back", "error"] as const),
  toast: oneOf(PARAMS.get("toast"), ["recluster", "renamed"] as const),
  reclustering: PARAMS.get("recluster") === "running",
};
