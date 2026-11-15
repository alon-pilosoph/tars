import { describe, expect, it } from "vitest";
import { Endpointer } from "./here";
import { laterPrompts, nextPrompt, promptState } from "./Record";
import type { Session } from "./types";

const session = (takes: string[], skipped: string[] = []): Session => ({
  kind: "voice",
  title: "Your voice",
  mic: "tars",
  device: "",
  person: "Alon",
  about: "",
  after: "",
  prompts: ["a", "b", "c"].map(id => ({ id, say: id, how: "", seconds: 0, countdown: 0 })),
  takes: takes.map((prompt, n) => ({ id: n + 1, prompt, seconds: 2, problem: null })),
  skipped,
  minutes: 1,
});

describe("nextPrompt", () => {
  it("is the first phrase with no take that wasn't skipped", () => {
    expect(nextPrompt(session([]))).toBe(0);
    expect(nextPrompt(session(["a"], ["b"]))).toBe(2);
    expect(nextPrompt(session(["b"]))).toBe(0); // one left behind comes first
  });
  it("is null once every phrase has a take or was skipped", () => {
    expect(nextPrompt(session(["a", "c"], ["b"]))).toBeNull();
  });
});

describe("Endpointer", () => {
  const room = -60;
  const run = (dbs: number[]) => {
    const e = new Endpointer();
    for (const [n, db] of dbs.entries()) {
      const stopped = e.feed(db);
      if (stopped) return { stopped, at: n };
    }
    return null;
  };
  const frames = (s: number, db: number) => Array<number>(Math.round(s / 0.02)).fill(db);

  it("ends a take at a second's pause after speech", () => {
    expect(run([...frames(0.5, room), ...frames(1, -20), ...frames(2, room)])).toEqual({ stopped: "silence", at: 124 });
  });
  it("doesn't end it at a short pause between words", () => {
    expect(run([...frames(0.5, room), ...frames(0.6, -20), ...frames(0.4, room), ...frames(0.6, -20)])).toBeNull();
  });
  it("gives up when nobody speaks", () => {
    expect(run(frames(7, room))?.stopped).toBe("nobody");
  });
  it("hears speech over a loud room only when it's well above it", () => {
    expect(run(frames(7, -30))?.stopped).toBe("nobody");
  });
});

describe("what's left for later", () => {
  it("is what was left for later and still has no take, in order", () => {
    expect(laterPrompts(session(["b"], ["c", "a", "b"]))).toEqual([0, 2]);
  });
  it("each phrase's state, for the list", () => {
    const s = {
      ...session(["a", "b"], ["c"]),
      takes: [
        { id: 1, prompt: "a", seconds: 2, problem: null },
        { id: 2, prompt: "b", seconds: 2, problem: "quiet" as const },
      ],
    };
    expect(s.prompts.map(p => promptState(s, p))).toEqual(["recorded", "redo", "later"]);
    expect(promptState(session([]), { id: "a", say: "a", how: "", seconds: 0, countdown: 0 })).toBe("todo");
  });
});
