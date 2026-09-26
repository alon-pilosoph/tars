import { DEMO } from "./params";

export interface Opts {
  method?: string;
  body?: string;
}

/** The server's reason, when it gave one: FastAPI sends {"detail": "..."}, or a list of problems for a bad body. */
async function reason(r: Response) {
  try {
    const { detail } = await r.json();
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail))
      return detail
        .map(d => d?.msg)
        .filter(Boolean)
        .join("; ");
  } catch {
    /* not JSON */
  }
  return r.statusText || `HTTP ${r.status}`;
}

async function request<T>(path: string, opts: Opts): Promise<T> {
  if (DEMO) return (await import("./demo")).demoApi(path, opts) as Promise<T>;
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(await reason(r));
  return r.json();
}

export const get = <T>(path: string) => request<T>(path, {});

// Changes go to the server one at a time, in the order they were made, and reads wait for them: two quick taps
// can't arrive out of order, and a Refresh never reads what was there before your last tap.
let writes: Promise<unknown> = Promise.resolve();
function write<T>(path: string, opts: Opts): Promise<T> {
  const done = writes.then(() => request<T>(path, opts));
  writes = done.catch(() => {});
  return done;
}
export const writesDone = () => writes;

export const post = <T = unknown>(path: string, body?: unknown) =>
  write<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
export const del = (path: string) => write(path, { method: "DELETE" });
