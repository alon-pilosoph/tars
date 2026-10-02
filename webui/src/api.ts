import { DEMO } from "./params";

export interface Opts {
  method?: string;
  body?: string;
  timeoutMs?: number;
}

const TIMEOUT_MS = 15_000; // a Pi that dropped off the Wi-Fi mustn't leave Refresh and every later change stuck

/** The server's reason, when it gave one: FastAPI sends {"detail": "..."}, or a list of problems for a bad body. */
async function reason(r: Response) {
  const text = await r.text().catch(() => "");
  try {
    const { detail } = JSON.parse(text);
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail))
      return detail
        .map(d => d?.msg)
        .filter(Boolean)
        .join("; ");
  } catch {
    /* not JSON */
  }
  return text.trim() || r.statusText || `HTTP ${r.status}`;
}

async function request<T>(path: string, { timeoutMs = TIMEOUT_MS, ...opts }: Opts): Promise<T> {
  if (DEMO) return (await import("./demo")).demoApi(path, opts) as Promise<T>;
  let r: Response;
  try {
    const signal = AbortSignal.timeout(timeoutMs);
    r = await fetch(path, { headers: { "Content-Type": "application/json" }, signal, ...opts });
  } catch (e) {
    if (e instanceof DOMException && e.name === "TimeoutError") throw new Error("TARS didn't answer in time");
    throw e;
  }
  if (!r.ok) throw new Error(await reason(r));
  return r.json();
}

export const getJson = <T>(path: string) => request<T>(path, {});

// Changes go to the server one at a time, in the order they were made, and reads wait for them: two quick taps
// can't arrive out of order, and a Refresh never reads what was there before your last tap.
let writes: Promise<unknown> = Promise.resolve();
let writeCount = 0;
function write<T>(path: string, opts: Opts): Promise<T> {
  writeCount++;
  const done = writes.then(() => request<T>(path, opts));
  writes = done.catch(() => {});
  return done;
}
export const writesDone = () => writes;
/** A load that saw this count change while it ran has older data than the page. */
export const writesMade = () => writeCount;

export const post = <T = unknown>(path: string, body?: unknown, timeoutMs?: number) =>
  write<T>(path, { method: "POST", body: JSON.stringify(body ?? {}), timeoutMs });
export const del = (path: string) => write(path, { method: "DELETE" });
