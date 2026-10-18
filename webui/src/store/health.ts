/* The Status page's data: loaded when the page is shown and with Refresh while it is, never with every page load,
   since it asks each cloud service with TARS's key (the server keeps the answers for a minute). */
import { getJson } from "../api";
import type { Health } from "../types";
import { set } from "./core";
import { errText } from "./ui";

let loading: Promise<void> | null = null;

export function loadHealth() {
  loading ??= (async () => {
    set({ healthLoading: true });
    try {
      set({ health: await getJson<Health>("/api/health"), healthError: "" });
    } catch (e) {
      set({ healthError: errText(e) || "Failed to fetch" });
    } finally {
      set({ healthLoading: false });
      loading = null;
    }
  })();
  return loading;
}
