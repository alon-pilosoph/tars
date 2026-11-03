import { describe, expect, it } from "vitest";
import { nextPrompt } from "./Record";
import type { Session } from "./types";

const session = (takes: string[], skipped: string[] = []): Session => ({
  kind: "voice",
  title: "Your voice",
  person: "Alon",
  about: "",
  after: "",
  prompts: ["a", "b", "c"].map(id => ({ id, say: id, how: "" })),
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
