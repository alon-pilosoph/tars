import { afterEach, describe, expect, it, vi } from "vitest";
import { post, writesDone, writesMade } from "./api";
import {
  failedLine,
  fmtSize,
  metricChange,
  personName,
  reminderEnded,
  reminderFor,
  reminderNow,
  reminderWhen,
  sentence,
  timingLine,
  wakeLine,
} from "./format";
import { md, plain } from "./markdown";
import { type State, forPerson, get, personShown } from "./store";
import { normalize, snapshotOf, within } from "./store/load";
import type { ApiConversation, Cluster, Item, Reminder, TarsEvent } from "./types";

describe("md", () => {
  it("escapes before adding any tag", () => {
    expect(md('<img src=x onerror="alert(1)"> **<b>**')).toBe(
      "<p>&lt;img src=x onerror=&quot;alert(1)&quot;&gt; <strong>&lt;b&gt;</strong></p>",
    );
  });
  it("formats paragraphs, headings, and both kinds of list", () => {
    expect(md("# Need\n- eggs\n- *milk*\n\n1. mix\n2. bake\nDone")).toBe(
      "<h4>Need</h4><ul><li>eggs</li><li><em>milk</em></li></ul><ol><li>mix</li><li>bake</li></ol><p>Done</p>",
    );
  });
  it("has a plain-text version for copying", () => {
    expect(plain("**Serves 6**, *about* an hour")).toBe("Serves 6, about an hour");
  });
});

describe("reminders", () => {
  const r = (x: Partial<Reminder>): Reminder => ({
    id: 1,
    created: 0,
    kind: "message",
    text: "hi",
    for_name: "stacey",
    from_name: "alon",
    set_via: "voice",
    conversation_id: null,
    due: null,
    needs_ack: true,
    repeat_every_s: 120,
    max_tries: 10,
    status: "scheduled",
    tries: 0,
    next_at: null,
    last_said: null,
    acked_at: null,
    acked_by: null,
    acked_via: null,
    says: "",
    ...x,
  });
  it("says where each one stands", () => {
    vi.setSystemTime(new Date("2026-09-26T10:00:00"));
    const at = (h: number, m = 0) => new Date(2026, 8, 26, h, m).getTime() / 1000;
    expect(reminderWhen(r({}))).toBe("When Stacey is next heard");
    expect(reminderWhen(r({ due: at(19, 30), next_at: at(19, 30) }))).toBe("Today, 7:30 PM");
    expect(reminderWhen(r({ due: at(33), next_at: at(33) }))).toBe("Tomorrow, 9:00 AM");
    expect(reminderWhen(r({ due: at(9), next_at: at(10, 10) }))).toBe("Snoozed until 10:10 AM");
    expect(reminderNow(r({ status: "waiting", tries: 2, next_at: at(10, 2) }))).toBe(
      "Said 2 of 10 times, again at 10:02 AM. Waiting for “got it”.",
    );
    expect(reminderEnded(r({ status: "acknowledged", acked_via: "voice", acked_by: "stacey", acked_at: at(9) }))).toBe(
      "Stacey said “got it” today at 9:00 AM.",
    );
    expect(reminderEnded(r({ status: "acknowledged", acked_via: "voice", acked_at: at(9) }))).toBe(
      "An unknown voice said “got it” today at 9:00 AM.",
    );
    expect(reminderEnded(r({ status: "acknowledged", acked_via: "web", acked_at: at(9) }))).toBe(
      "Got it on this page today at 9:00 AM.",
    );
    expect(reminderEnded(r({ status: "missed", tries: 1 }))).toBe("Missed. Said 1 time and nobody said “got it”.");
    expect(reminderEnded(r({ status: "cancelled", due: at(9) }))).toBe("Stopped. It was due today at 9:00 AM.");
    const timer = { kind: "timer" as const, repeat_every_s: 10 };
    expect(reminderNow(r({ ...timer, status: "waiting", due: at(9, 58), tries: 12 }))).toBe("Ringing since 9:58 AM");
    expect(reminderEnded(r({ ...timer, status: "missed", tries: 90 }))).toBe(
      "Missed. It rang for 15 minutes and nobody turned it off.",
    );
    vi.useRealTimers();
  });
  it("says who it's for and from", () => {
    expect(reminderFor(r({}))).toBe("Message for Stacey, from Alon");
    expect(reminderFor(r({ kind: "timer", for_name: null, from_name: null }))).toBe("Timer for whoever's there");
    expect(personName(" mary  ann ")).toBe("Mary Ann");
  });
});

describe("format", () => {
  it("says how long an answer took, who wrote it, and why it failed", () => {
    const base = { id: 1, ts: 0, role: "tars", text: "Late." } as const;
    expect(timingLine({ ...base, timings: { total: 1.43, stt: 0.1 }, answered_by: "quick" })).toBe(
      "Took 1.4 s from the end of speech to the first sound: 0.10 s to write down what was said. " +
        "Answered by the quick model (Qwen).",
    );
    expect(timingLine({ ...base, answered_by: "fallback", error: "APITimeoutError" })).toBe(
      "Answered by OpenAI, as a backup. The error was “APITimeoutError”.",
    );
    expect(timingLine(base)).toBe("");
    expect(failedLine({ ...base, failed_at: "tts" })).toBe("Failed while saying the answer.");
    expect(failedLine(base)).toBe("");
  });
  it("says what TARS heard and whose voice it was", () => {
    const wake = { event_id: 1, heard: "hey [unk]", confidence: 0.18, outcome: "ask", label: null, cluster_id: 3 };
    expect(wakeLine({ ...wake, outcome: "answer" } as never, { name: "Alon", known: true })).toBe(
      "TARS heard “hey …” and was 18% sure. It recognised Alon by voice.",
    );
    expect(wakeLine(wake as never, { name: null, known: false })).toBe(
      "TARS heard “hey …”, was only 18% sure, so it asked “Did you call me?” first. It didn't recognise the voice.",
    );
  });
  it("ends a sentence once", () => {
    expect(sentence("a request followed")).toBe("A request followed.");
    expect(sentence("they answered “Did you call me?”")).toBe("They answered “Did you call me?”");
  });
  it("sizes files", () => {
    expect([fmtSize(null), fmtSize(900), fmtSize(356352), fmtSize(12884901)]).toEqual([
      "",
      "900 B",
      "348 KB",
      "12.3 MB",
    ]);
  });
  it("says whether a test got better, counting fewer false answers as better", () => {
    expect(metricChange({ name: "hey TARS", current: "9 of 12", candidate: "11 of 12" })?.verdict).toBe("better");
    expect(metricChange({ name: "TV", current: "1.8%", candidate: "2.4%", lower_is_better: true })?.verdict).toBe(
      "worse",
    );
    expect(metricChange({ name: "TV", current: "0.0", candidate: "0.0", lower_is_better: true })?.verdict).toBe("same");
    expect(metricChange({ name: "odd", current: "n/a", candidate: "1" })).toBeNull();
  });
});

describe("loading", () => {
  const item = (id: number, seen: boolean) => ({ id, seen }) as Item;
  const ev = (id: number, follow: TarsEvent["follow"], label: TarsEvent["label"]) =>
    ({ id, follow, label }) as TarsEvent;
  const convs = [
    { id: 1, turns: [{ id: 10, items: [item(7, false), 8] }, { id: 11 }] },
  ] as unknown as ApiConversation[];

  it("keeps each sent item once, and ids in the turns", () => {
    const { convs: c, items } = normalize(convs, [item(8, true)]);
    expect(c[0].turns[0].items).toEqual([7, 8]);
    expect(items.map(i => i.id)).toEqual([8, 7]);
  });

  it("a reload shows only what the last Refresh showed, as it is now", () => {
    const first = { ...normalize(convs, []), events: [ev(1, "said_nothing", null), ev(2, "asked", null)] };
    const snap = snapshotOf(first);
    expect([...snap.newItems]).toEqual([7]);
    expect([...snap.review]).toEqual([1]);
    const laterConvs = [
      { id: 1, turns: [{ id: 10 }, { id: 11 }, { id: 12 }] },
      { id: 2, turns: [] },
    ];
    const later = normalize(laterConvs as unknown as ApiConversation[], [item(7, true), item(9, false)]);
    const shown = within(snap, { ...later, events: [ev(1, "said_nothing", "real"), ev(3, null, null)] });
    expect(shown.convs.map(c => [c.id, c.turns.map(t => t.id)])).toEqual([[1, [10, 11]]]);
    expect(shown.items.map(i => [i.id, i.seen])).toEqual([[7, true]]);
    expect(shown.events.map(e => [e.id, e.label])).toEqual([[1, "real"]]);
  });
});

describe("the person filter", () => {
  const voice = (id: number, name: string | null, kind: Cluster["kind"] = "person") =>
    ({ id, name, kind, size: 1, samples: [] }) as Cluster;
  const state = (person: State["person"], clusters: Cluster[]): State => ({ ...get(), person, clusters });
  const alon = voice(1, "Alon");
  const stacey = voice(2, "Stacey");

  it("Home applies a person only with two or more people, and never Household", () => {
    expect(personShown(state(1, [alon, stacey]), false)).toBe(1);
    expect(personShown(state(1, [alon]), false)).toBe("all");
    expect(personShown(state("household", [alon, stacey]), false)).toBe("all");
  });
  it("Sent applies one person, or Household", () => {
    expect(personShown(state(1, [alon]), true)).toBe(1);
    expect(personShown(state("household", [alon]), true)).toBe("household");
  });
  it("a voice that's no longer a named person matches nothing", () => {
    expect(personShown(state(3, [alon, stacey, voice(3, null)]), true)).toBe("all");
    expect(personShown(state(4, [alon, stacey, voice(4, "TV", "not_person")]), true)).toBe("all");
  });
  it("household things are only under Household, and a person's only under them", () => {
    const house = { scope: "household", for: { cluster_id: 1, name: "Alon" } } as Item;
    const mine = { scope: "person", for: { cluster_id: 1, name: "Alon" } } as Item;
    expect([forPerson(house, "household"), forPerson(house, 1), forPerson(house, "all")]).toEqual([true, false, true]);
    expect([forPerson(mine, "household"), forPerson(mine, 1), forPerson(mine, 2)]).toEqual([false, true, false]);
  });
});

describe("changes", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reach the server one at a time, in the order they were made, even when the first is slow", async () => {
    const arrived: string[] = [];
    vi.stubGlobal("fetch", async (path: string) => {
      if (path === "/first") await new Promise(r => setTimeout(r, 30));
      arrived.push(path);
      return new Response("{}");
    });
    const before = writesMade();
    await Promise.all([post("/first"), post("/second")]);
    await writesDone();
    expect(arrived).toEqual(["/first", "/second"]);
    expect(writesMade() - before).toBe(2);
  });

  it("a failed change says why, from a JSON detail or a plain-text body", async () => {
    vi.stubGlobal("fetch", async (path: string) =>
      path === "/json"
        ? new Response(JSON.stringify({ detail: "no such voice" }), { status: 404 })
        : new Response("changes can only come from the TARS page itself", { status: 403 }),
    );
    await expect(post("/json")).rejects.toThrow("no such voice");
    await expect(post("/text")).rejects.toThrow("changes can only come from the TARS page itself");
  });
});
