/* The Status page's data: loaded when the page is shown and with Refresh while it is, never with every page load,
   since it asks each cloud service with TARS's key (the server keeps the answers for a minute). */
import { getJson } from "../api";
import type { Health } from "../types";
import { set } from "./core";
import { errText } from "./ui";

// The server gives the services 10 s at most; the first ask in a minute can take that long.
const HEALTH_TIMEOUT_MS = 30_000;

let loading: Promise<void> | null = null;

export function loadHealth() {
  loading ??= (async () => {
    try {
      set({ health: await getJson<Health>("/api/health", { timeoutMs: HEALTH_TIMEOUT_MS }), healthError: "" });
    } catch (e) {
      set({ healthError: errText(e) || "Failed to fetch" });
    } finally {
      loading = null;
    }
  })();
  return loading;
}

/** Once the Status page's load, if one is under way, has landed. */
export const healthLoaded = () => loading ?? Promise.resolve();
