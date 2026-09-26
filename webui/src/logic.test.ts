import { describe, expect, it } from "vitest";
import { fold } from "./fold";
import { fmtSize, sentence } from "./format";
import { md, plain } from "./markdown";
import { normalize, snapshotOf, within } from "./store/load";
import type { ApiConversation, Item, TarsEvent } from "./types";

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
    expect(sentence("they answered 'Did you call me?'")).toBe("They answered 'Did you call me?'");
  });
  it("sizes files", () => {
    expect([fmtSize(null), fmtSize(900), fmtSize(356352), fmtSize(12884901)]).toEqual([
      "",
      "900 B",
      "348 KB",
      "12.3 MB",
    ]);
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
    const later = normalize(
      [
        { id: 1, turns: [{ id: 10 }, { id: 11 }, { id: 12 }] },
        { id: 2, turns: [] },
      ] as unknown as ApiConversation[],
      [item(7, true), item(9, false)],
    );
    const shown = within(snap, { ...later, events: [ev(1, "said_nothing", "real"), ev(3, null, null)] });
    expect(shown.convs.map(c => [c.id, c.turns.map(t => t.id)])).toEqual([[1, [10, 11]]]);
    expect(shown.items.map(i => [i.id, i.seen])).toEqual([[7, true]]);
    expect(shown.events.map(e => [e.id, e.label])).toEqual([[1, "real"]]);
  });
});
