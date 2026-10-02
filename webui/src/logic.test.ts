import { afterEach, describe, expect, it, vi } from "vitest";
import { post, writesDone, writesMade } from "./api";
import { fold } from "./fold";
import { fmtSize, metricChange, sentence } from "./format";
import { md, plain } from "./markdown";
import { type State, forPerson, get, personShown } from "./store";
import { normalize, snapshotOf, within } from "./store/load";
import type { ApiConversation, Cluster, Item, TarsEvent } from "./types";

describe("md", () => {
  it("escapes before adding any tag", () => {
    expect(md('<img src=x onerror="alert(1)"> **<b>**')).toBe(
      "<p>&lt;img src=x onerror=&quot;alert(1)&quot;&gt; <b>&lt;b&gt;</b></p>",
    );
  });
  it("formats paragraphs, headings, and both kinds of list", () => {
    expect(md("# Need\n- eggs\n- *milk*\n\n1. mix\n2. bake\nDone")).toBe(
      "<h4>Need</h4><ul><li>eggs</li><li><i>milk</i></li></ul><ol><li>mix</li><li>bake</li></ol><p>Done</p>",
    );
  });
  it("has a plain-text version for copying", () => {
    expect(plain("**Serves 6**, *about* an hour")).toBe("Serves 6, about an hour");
  });
});

describe("format", () => {
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

describe("fold", () => {
  const turns = [1, 2, 3, 4, 5, 6, 7];
  it("shows short conversations whole", () => {
    expect(fold([1, 2, 3, 4], false, () => false)).toEqual({ head: [1, 2, 3, 4], hidden: 0, tail: [] });
  });
  it("folds the middle, keeping the first two, the last, and any turn that must show", () => {
    expect(fold(turns, false, t => t === 5)).toEqual({ head: [1, 2], hidden: 3, tail: [5, 7] });
  });
  it("shows everything once opened", () => {
    expect(fold(turns, true, () => false).hidden).toBe(0);
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
