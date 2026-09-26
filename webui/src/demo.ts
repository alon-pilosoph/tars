/* Demo data (only with ?demo): the design's sample household, so every state can be opened from a link. */
import type { Opts } from "./api";
import { LINK } from "./params";
import type { Cluster, Conversation, Item, Label, Models, RetrainResult, Speaker, TarsEvent, Turn } from "./types";

type Row = [
  number,
  TarsEvent["kind"],
  TarsEvent["outcome"],
  number,
  string?,
  number?,
  (string | null)?,
  TarsEvent["follow"]?,
  (string | null)?,
  (number | null)?,
  number?,
  (Label | null)?,
];
type DemoEvent = Omit<TarsEvent, "auto_label" | "auto_reason">;
type DemoTurn = Omit<Turn, "items"> & { items?: number[] };
type DemoConv = Omit<Conversation, "turns"> & { turns: DemoTurn[] };

let D: {
  events: DemoEvent[];
  clusters: Omit<Cluster, "size">[];
  convs: DemoConv[];
  items: Item[];
  models: Models;
} | null = null;

function demoInit() {
  const now = Date.now() / 1000,
    empty = LINK.demo === "empty";
  const ago = (m: number) => now - m * 60;
  const yday = (h: number, m: number) => {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    d.setHours(h, m, 0, 0);
    return d.getTime() / 1000;
  };
  // wake events: [minutes ago, kind, outcome, score, heard, conf, transcript, follow, speaker, cluster, pinned, label]
  const rows: Row[] = [
    [
      400,
      "wake",
      "answer",
      0.92,
      "hey tars",
      0.97,
      "What's the weather going to be like tomorrow?",
      "asked",
      "alon",
      1,
    ],
    [355, "wake", "answer", 0.92, "hey tars", 0.97, "Set a timer for twelve minutes.", "asked", "alon", 1],
    [330, "wake", "answer", 0.88, "hey tars", 0.9, "Play some jazz.", "asked", null, 2],
    [310, "wake", "answer", 0.92, "hey tars", 0.97, "Remind me to call Stacey after dinner.", "asked", "alon", 1],
    [280, "wake", "answer", 0.88, "hey tars", 0.9, "Add eggs, milk and coffee to the shopping list.", "asked", null, 2],
    [265, "wake", "answer", 0.92, "hey tars", 0.97, "Send me everything for the Galilee trip.", "asked", "alon", 1],
    [250, "wake", "answer", 0.81, "hey darts", 0.74, "Is it going to rain?", "asked", null, 3],
    [
      230,
      "wake",
      "answer",
      0.88,
      "hey tars",
      0.9,
      "Can you send me the lasagna recipe from last Sunday?",
      "asked",
      null,
      2,
    ],
    [219, "wake", "answer", 0.81, "hey darts", 0.74, "What time is it in Tokyo?", "asked", null, 3],
    [
      200,
      "wake",
      "answer",
      0.63,
      "hey tars",
      0.41,
      "and in tonight's top story the storm is moving east",
      "not_for_us",
      null,
      4,
    ],
    [180, "wake", "answer", 0.63, "hey tars", 0.41, "you won't believe what happened next", "not_for_us", null, 5],
    [150, "wake", "answer", 0.86, "hey tars", 0.93, null, "said_nothing"],
    [100, "wake", "ask", 0.66, "hey bars", 0.12, null, "said_nothing"],
    [90, "wake", "ask", 0.64, "hey mars", 0.1, "No.", "not_for_us", "alon", 1],
    [80, "wake", "ignore", 0.58, "hey stars", 0.04],
    [70, "wake", "ignore", 0.55, "[unk] stars", 0.02],
    [60, "wake", "ignore", 0.73, "bars", 0.16, null, null, null, null, 0, "real"],
    [40, "near_miss", null, 0.41],
    [39.95, "wake", "answer", 0.9, "hey tars", 0.95, "Turn off the lights.", "asked", "alon", 1, 1],
    [12, "near_miss", null, 0.36],
  ];
  const events: DemoEvent[] = empty
    ? []
    : rows.map((r, i) => ({
        id: i + 1,
        ts: ago(r[0]),
        kind: r[1],
        outcome: r[2],
        wake_score: r[3],
        heard: r[4] ?? null,
        confidence: r[5] ?? null,
        transcript: r[6] ?? null,
        follow: r[7] ?? null,
        speaker: r[8] ?? null,
        cluster_id: r[9] ?? null,
        cluster_pinned: r[10] || 0,
        label: r[11] ?? null,
        audio: "demo",
        utterance_audio: r[6] ? "demo" : null,
      }));
  if (LINK.reviewDone)
    events.forEach(e => {
      if (e.follow !== "asked" && !e.label)
        e.label = e.follow === "not_for_us" || e.outcome === "ignore" ? "not_real" : "real";
    });
  const clusters: Omit<Cluster, "size">[] = empty
    ? []
    : [
        { id: 1, name: "Alon", kind: "person" },
        { id: 2, name: "Stacey", kind: "person" },
        { id: 3, name: null, kind: "unknown" },
        { id: 4, name: null, kind: "not_person" },
        { id: 5, name: null, kind: "unknown" },
      ];
  const ALON: Speaker = { cluster_id: 1, name: "Alon" },
    STACEY: Speaker = { cluster_id: 2, name: "Stacey" },
    V3: Speaker = { cluster_id: 3, name: null };
  const map = `<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360'><defs><pattern id='p' width='14' height='14' patternUnits='userSpaceOnUse' patternTransform='rotate(45)'><rect width='14' height='14' fill='#d8d5ce'/><rect width='7' height='14' fill='#cdc9c1'/></pattern></defs><rect width='640' height='360' fill='url(#p)'/><text x='320' y='186' font-family='monospace' font-size='18' text-anchor='middle' fill='#5f5c56'>campsite map (photo)</text></svg>`;
  const items: Item[] = [
    {
      id: 101,
      kind: "note",
      title: "Lasagna (the one from last Sunday)",
      scope: "person",
      for: STACEY,
      seen: false,
      conversation_id: 1,
      turn_id: 12,
      ts: ago(20),
      body: "**Serves 6, about 1 h 20 min**\n\n# You need\n- 12 lasagna sheets\n- 500 g ricotta\n- 400 g spinach\n- 2 jars of tomato sauce\n- 250 g mozzarella\n\n# Method\n1. Oven to 190°C.\n2. Wilt the spinach, squeeze it dry, mix it into the ricotta.\n3. Layer sauce, sheets, ricotta. Repeat three times.\n4. Mozzarella on top. 45 min covered, 15 uncovered.\n\nLet it rest 10 minutes. *It's better the next day.*",
    },
    {
      id: 102,
      kind: "link",
      title: "Line 18 timetable, Herzl St stop",
      site: "transit.example.org",
      url: "https://transit.example.org/lines/18/herzl",
      description: "Weekday departures from Herzl St. Next ones: 7:52 and 8:07.",
      scope: "person",
      for: ALON,
      seen: false,
      conversation_id: 2,
      turn_id: 28,
      ts: ago(46),
    },
    {
      id: 103,
      kind: "list",
      title: "Shopping list",
      scope: "household",
      for: STACEY,
      seen: true,
      conversation_id: 3,
      turn_id: 32,
      ts: ago(80),
      entries: [
        { text: "Bread", done: true },
        { text: "Tomatoes", done: true },
        { text: "Olive oil", done: true },
        { text: "Oat milk", done: false },
        { text: "Eggs", done: false },
        { text: "Milk", done: false },
        { text: "Coffee", done: false },
      ],
    },
    {
      id: 104,
      kind: "link",
      title: "Kfar Blum campsite, booking #4471",
      site: "campsites.example.org",
      url: "https://campsites.example.org/booking/4471",
      description: "Friday to Sunday, pitch 17. Check-in from 2 PM, quiet hours after 11.",
      scope: "person",
      for: ALON,
      seen: true,
      conversation_id: 7,
      turn_id: 72,
      ts: ago(210),
    },
    {
      id: 105,
      kind: "note",
      title: "Directions to the campsite",
      scope: "person",
      for: ALON,
      seen: true,
      conversation_id: 7,
      turn_id: 72,
      ts: ago(210),
      body: "About **2 h 40 min** from home.\n\n1. Route 6 north to the end.\n2. Route 90 through Rosh Pina.\n3. Right at the Kfar Blum junction, then follow the brown signs.\n\nLast fuel is at Rosh Pina.",
    },
    {
      id: 106,
      kind: "list",
      title: "Galilee packing list",
      scope: "person",
      for: ALON,
      seen: true,
      conversation_id: 7,
      turn_id: 72,
      ts: ago(210),
      entries: [
        "Tent and pegs",
        "Sleeping bags ×3",
        "Head torches",
        "Gas stove + 2 canisters",
        "Water filter",
        "First-aid kit",
        "Swimsuits",
        "Sunscreen",
        "Cards",
      ].map((t, n) => ({ text: t, done: n < 2 })),
    },
    {
      id: 107,
      kind: "file",
      title: "Campsite map",
      name: "kfar-blum-map.jpg",
      mime: "image/jpeg",
      size: 1258291,
      preview: "data:image/svg+xml," + encodeURIComponent(map),
      scope: "person",
      for: ALON,
      seen: false,
      conversation_id: 7,
      turn_id: 72,
      ts: ago(210),
    },
    {
      id: 108,
      kind: "file",
      title: "Dishwasher warranty",
      name: "dishwasher-warranty.pdf",
      mime: "application/pdf",
      size: 356352,
      scope: "person",
      for: STACEY,
      seen: true,
      conversation_id: 8,
      turn_id: 82,
      ts: yday(21, 3),
    },
  ];
  if (LINK.seenAll)
    items.forEach(i => {
      i.seen = true;
    });
  type T = [
    string,
    string,
    { rating?: "good" | "bad"; items?: number[]; speaker?: Speaker; fix?: string; aside?: boolean }?,
  ];
  const conv = (id: number, started: number, speaker: Speaker, wake: DemoConv["wake"], turns: T[]): DemoConv => {
    const ts: DemoTurn[] = turns.map((t, n) => ({
      id: id * 10 + n + 1,
      ts: started + n * 8,
      role: t[0] === "T" ? "tars" : "person",
      text: t[1],
      ...(t[0] === "T"
        ? { rating: t[2]?.rating ?? null, items: t[2]?.items || [] }
        : {
            has_audio: true,
            speaker: t[2]?.speaker ?? speaker,
            corrected_text: t[2]?.fix ?? null,
            not_for_tars: !!t[2]?.aside,
          }),
    }));
    return {
      id,
      started,
      ended: started + turns.length * 8,
      speaker,
      wake,
      turns: ts,
      preview: turns.find(t => t[0] === "P")?.[1] || "",
      item_count: ts.reduce((a, t) => a + (t.items?.length || 0), 0),
    };
  };
  const W = (heard: string, confidence: number, outcome: "answer" | "ask" = "answer") => ({
    event_id: null,
    heard,
    confidence,
    outcome,
  });
  const convs: DemoConv[] = empty
    ? []
    : [
        conv(1, ago(21), STACEY, W("hey tars", 0.96), [
          ["P", "Can you send me the lasagna recipe from last Sunday?"],
          ["T", "Sent it. It's on the TARS page.", { items: [101] }],
        ]),
        conv(2, ago(52), ALON, W("hey tars", 0.97), [
          ["P", "What's the weather tomorrow morning?"],
          ["T", "Cloudy, 14 degrees. Rain from about four."],
          ["P", "Should I take the bike?"],
          ["T", "I'd take the bus. The rain is heaviest around six."],
          ["P", "When's the next 18 from Herzl?"],
          ["T", "7:52, then 8:07."],
          ["P", "Send me the timetable."],
          ["T", "Sent it. It's on the TARS page.", { items: [102] }],
        ]),
        conv(3, ago(82), STACEY, W("hey tars", 0.94), [
          ["P", "Add eggs, milk and toffee to the shopping list."],
          ["T", "Added eggs, milk and toffee. The list has seven things.", { items: [103] }],
        ]),
        conv(4, ago(105), ALON, W("hey tars", 0.95), [
          ["P", "Set a timer for twelve minutes."],
          ["T", "Twelve minutes, starting now."],
          ["P", "Stacey, can you check the oven?", { aside: true }],
          ["P", "It's fine, leave it.", { aside: true, speaker: STACEY }],
        ]),
        conv(5, ago(130), ALON, W("hey cars", 0.18, "ask"), [
          ["T", "Did you call me?"],
          ["P", "Yes, set an alarm for seven."],
          ["T", "Alarm set for 7:00 tomorrow morning."],
        ]),
        conv(6, ago(160), V3, W("hey darts", 0.74), [
          ["P", "What time is it in Tokyo?"],
          ["T", "12:40 at night. Tomorrow already, over there."],
        ]),
        conv(7, ago(212), ALON, W("hey tars", 0.97), [
          ["P", "Send me everything for the Galilee trip."],
          [
            "T",
            "Sent four things: the booking, directions, the packing list and the campsite map. They're on the TARS page.",
            { items: [104, 105, 106, 107] },
          ],
        ]),
        conv(8, yday(21, 2), STACEY, W("hey tars", 0.95), [
          ["P", "Send me the warranty for the dishwasher."],
          ["T", "Sent it. It's a PDF, on the TARS page.", { items: [108] }],
        ]),
        conv(9, yday(20, 15), ALON, W("hey tars", 0.97), [
          ["P", "Tell me something about black holes."],
          [
            "T",
            "If you fell into one, you'd see the whole future of the universe flash past. Probably. Nobody's come back to confirm.",
            { rating: "good" },
          ],
        ]),
        conv(10, yday(19, 40), ALON, W("hey tars", 0.97), [
          ["P", "Remind me to call Tracy after dinner.", { fix: "Remind me to call Stacey after dinner." }],
          ["T", "I'll remind you to call Tracy at 8:30.", { rating: "bad" }],
        ]),
        conv(11, yday(18, 0), STACEY, W("hey tars", 0.9), [
          ["P", "Is it going to rain?"],
          ["T", "Not today. Clear until Thursday."],
        ]),
      ];
  D = {
    events,
    clusters,
    convs,
    items: empty ? [] : items,
    models: {
      active: {
        wake_model: "models/generic/hey_tars.tflite",
        threshold: 0.5,
        check_window_s: 3.0,
        check_model: empty ? "models/generic/hey_tars_check.json" : "voice_data/events/checks/generic/v2.json",
      },
      history: [
        ...(empty
          ? []
          : [
              {
                version: "check v2",
                ts: now - 3600 * 5,
                active: true,
                note: "Retrained on 43 of your wakes (37 real, 6 not).",
              },
              {
                version: "check v1",
                ts: now - 3600 * 30,
                active: false,
                note: "Retrained on 21 of your wakes (18 real, 3 not).",
              },
            ]),
        {
          version: "installed",
          ts: now - 3600 * 24 * 12,
          active: empty,
          note: "What TARS was installed with (models/generic/hey_tars_check.json).",
        },
      ],
      last_retrain: null,
      trainable: empty ? 0 : 52,
    },
  };
}

function demoAuto(e: DemoEvent, answered: number[]): [Label | null, string] {
  if (e.kind === "near_miss")
    return answered.some(t => t - e.ts > 0 && t - e.ts <= 6 * 60)
      ? ["real", "a real wake followed within seconds, so it was probably a missed hey TARS"]
      : [null, "near-miss"];
  if (e.outcome === "ignore") return ["not_real", "the double-check heard something else"];
  if (e.follow === "asked")
    return ["real", e.outcome === "answer" ? "a request followed" : "they answered 'Did you call me?'"];
  if (e.follow === "not_for_us") return ["not_real", "the reply wasn't meant for TARS"];
  if (e.follow === "said_nothing")
    return e.outcome === "ask"
      ? ["not_real", "nobody answered 'Did you call me?'"]
      : [null, "it woke, but nobody spoke"];
  return [null, ""];
}

/** ?demo=long: the same household with everything long, to see what wraps, clips or overflows. */
function stress(d: NonNullable<typeof D>) {
  const LONG_A = "Maximiliano Alessandro",
    LONG_S = "Anastasia-Konstantina";
  const rename = (sp: Speaker | null | undefined) => {
    if (sp?.name === "Alon") sp.name = LONG_A;
    if (sp?.name === "Stacey") sp.name = LONG_S;
  };
  d.clusters.forEach(c => {
    if (c.name === "Alon") c.name = LONG_A;
    if (c.name === "Stacey") c.name = LONG_S;
  });
  d.clusters.push(
    ...Array.from({ length: 6 }, (_, k) => ({
      id: 6 + k,
      name: k === 0 ? "Great-aunt Wilhelmina from Rotterdam" : null,
      kind: "unknown" as const,
    })),
  );
  d.convs.forEach(c => {
    rename(c.speaker);
    c.turns.forEach(t => rename(t.speaker));
  });
  d.items.forEach(i => rename(i.for));
  const url =
    "https://www.example.org/recipes/2026/09/the-absolutely-definitive-guide-to-slow-cooked-lasagna-with-spinach-and-ricotta?utm_source=tars&ref=kitchen";
  const long =
    "Could you please remind me tomorrow morning, before I leave for work but after I've had my coffee, to call the plumber about the dripping kitchen tap, and also to buy more of that oat milk Stacey likes, and to check whether the recycling goes out on Thursday or Friday this week?";
  const reply =
    "Tomorrow at 7:40, between coffee and leaving: call the plumber about the kitchen tap. I've added oat milk to the shopping list. Recycling goes out on Thursday this week, because Friday is a public holiday, which the council mentioned in a letter nobody read. I'd put a sticky note on the door as well. Belt and braces.";
  d.items.push(
    {
      id: 201,
      kind: "link",
      title:
        "The absolutely definitive guide to slow-cooked lasagna with spinach, ricotta and three kinds of cheese, tested forty times",
      site: "a-very-long-food-blog-name.example.org",
      url,
      description: `Everything you need, including the sauce, the resting time and why you should never skip it. Found at ${url}`,
      scope: "person",
      for: { cluster_id: 1, name: LONG_A },
      seen: false,
      conversation_id: 12,
      turn_id: 122,
      ts: Date.now() / 1000 - 60,
    },
    {
      id: 202,
      kind: "list",
      title: "Everything for the big family barbecue on Saturday afternoon at the park by the river",
      scope: "household",
      for: null,
      seen: false,
      conversation_id: 12,
      turn_id: 122,
      ts: Date.now() / 1000 - 60,
      entries: Array.from({ length: 30 }, (_, k) => ({
        text:
          k % 5 === 0
            ? `Item ${k + 1}: a much longer entry that goes on about what exactly to buy and from which shop`
            : `Item ${k + 1}`,
        done: k % 3 === 0,
      })),
    },
    {
      id: 203,
      kind: "file",
      title: "Quarterly household budget and renovation plan, final version",
      name: "quarterly-household-budget-and-renovation-plan-final-v3-really-final-this-time.pdf",
      mime: "application/pdf",
      size: 12884901,
      scope: "person",
      for: { cluster_id: 2, name: LONG_S },
      seen: false,
      conversation_id: 12,
      turn_id: 122,
      ts: Date.now() / 1000 - 60,
    },
    {
      id: 204,
      kind: "note",
      title: "Supercalifragilisticexpialidocious-and-other-very-long-words-that-never-break",
      scope: "person",
      for: { cluster_id: 1, name: LONG_A },
      seen: false,
      conversation_id: 12,
      turn_id: 122,
      ts: Date.now() / 1000 - 60,
      body:
        "# A long heading that keeps going and going well past the width of the card\n\n" +
        "Pneumonoultramicroscopicsilicovolcanoconiosis ".repeat(3) +
        "\n\n" +
        Array.from(
          { length: 12 },
          (_, k) => `- Step ${k + 1}: do the thing carefully, then check it twice before moving on`,
        ).join("\n"),
    },
  );
  d.convs.unshift({
    id: 12,
    started: Date.now() / 1000 - 90,
    ended: Date.now() / 1000 - 30,
    speaker: { cluster_id: 1, name: LONG_A },
    wake: {
      event_id: null,
      heard: "hey tars can you hear me over the dishwasher",
      confidence: 0.61,
      outcome: "answer",
    },
    preview: long,
    turns: [
      {
        id: 121,
        ts: Date.now() / 1000 - 90,
        role: "person",
        text: long,
        has_audio: true,
        speaker: { cluster_id: 1, name: LONG_A },
        corrected_text: null,
        not_for_tars: false,
      },
      { id: 122, ts: Date.now() / 1000 - 80, role: "tars", text: reply, rating: null, items: [201, 202, 203, 204] },
      {
        id: 123,
        ts: Date.now() / 1000 - 70,
        role: "person",
        text: `Also send me this ${url}`,
        has_audio: true,
        speaker: { cluster_id: 7, name: null },
        corrected_text: null,
        not_for_tars: true,
      },
    ],
  });
  d.events.unshift({
    id: 99,
    ts: Date.now() / 1000 - 100,
    kind: "wake",
    outcome: "ask",
    wake_score: 0.7,
    heard: "hey tars can you hear me over the dishwasher",
    confidence: 0.3,
    transcript: long,
    follow: "not_for_us",
    speaker: "maximiliano",
    cluster_id: 6,
    cluster_pinned: 1,
    label: null,
    audio: "demo",
    utterance_audio: "demo",
  });
  d.models.active.wake_model = "models/generic/experiments/2026-09-26/hey_tars_v3real_accent_long_window_final.tflite";
}

export const MOCK_TOASTS = {
  recluster:
    "7 requests grouped into 3 voices (1 new, 2 moved; your manual choices kept). Voiceprints updated for: Alon.",
  renamed: "Saved. 2 more requests for Stacey's voiceprint.",
};

const LOW = { lower_is_better: true };
export const MOCK_RETRAIN: Record<string, RetrainResult> = {
  skipped: {
    status: "skipped",
    summary:
      "Not enough to learn from yet: 3 labeled wakes, and every fifth is kept aside to test on. Keep talking to TARS, and answer the wakes on the Review tab.",
  },
  better: {
    status: "swapped",
    version: "check v3",
    labeled: 48,
    metrics: [
      { name: "Your held-out hey TARS", current: "9 of 12", candidate: "11 of 12" },
      {
        name: "Your held-out wakes that weren't for TARS, let through",
        current: "1 of 3",
        candidate: "0 of 3",
        ...LOW,
      },
      { name: "Held-out voices, quiet", current: "94.4%", candidate: "94.4%" },
      { name: "Held-out voices, TV and chatter", current: "80.6%", candidate: "80.8%" },
      { name: "Lookalikes let through", current: "1.8%", candidate: "1.8%", ...LOW },
      { name: "False answers per hour, TV", current: "0.0", candidate: "0.0", ...LOW },
      { name: "False answers per hour, audiobooks", current: "0.0", candidate: "0.0", ...LOW },
    ],
  },
  worse: {
    status: "kept",
    labeled: 12,
    metrics: [
      { name: "Your held-out hey TARS", current: "2 of 3", candidate: "3 of 3" },
      { name: "Held-out voices, quiet", current: "94.4%", candidate: "93.3%" },
      { name: "Held-out voices, TV and chatter", current: "80.6%", candidate: "79.4%" },
      { name: "Lookalikes let through", current: "1.8%", candidate: "3.6%", ...LOW },
      { name: "False answers per hour, TV", current: "0.0", candidate: "1.0", ...LOW },
      { name: "False answers per hour, audiobooks", current: "0.0", candidate: "0.0", ...LOW },
    ],
  },
};

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));

export async function demoApi(path: string, opts: Opts = {}): Promise<unknown> {
  if (!D) {
    demoInit();
    if (LINK.demo === "long") stress(D!);
  }
  const d = D!;
  await sleep(60);
  const m = opts.method || "GET",
    b = opts.body ? JSON.parse(opts.body) : {},
    ok = { ok: true };
  const size = (id: number) => d.events.filter(e => e.cluster_id === id).length;
  const withItems = (c: DemoConv) => ({
    ...clone(c),
    unseen_count: c.turns.flatMap(t => t.items || []).filter(id => !d.items.find(i => i.id === id)?.seen).length,
    turns: c.turns.map(t => ({ ...clone(t), items: (t.items || []).filter(id => d.items.find(i => i.id === id)) })),
  });
  let x: RegExpMatchArray | null;
  const url = new URL(path, "http://x"),
    p = url.pathname;
  if (p === "/api/events") {
    const ans = d.events.filter(e => e.kind === "wake" && e.outcome === "answer").map(e => e.ts);
    return d.events
      .map(e => {
        const [al, ar] = demoAuto(e, ans);
        return { ...e, auto_label: al, auto_reason: ar };
      })
      .sort((a, b) => b.ts - a.ts);
  }
  if (p === "/api/clusters" && m === "GET")
    return d.clusters.map(c => ({ ...c, size: size(c.id) })).sort((a, b) => b.size - a.size);
  if (p === "/api/models") return d.models;
  if (p === "/api/status") {
    return {
      events: d.events.length,
      labeled: d.events.filter(e => e.label).length,
      clustering: true,
      unseen_items: d.items.filter(i => !i.seen).length,
    };
  }
  if (p === "/api/conversations") return d.convs.map(withItems).sort((a, b) => b.started - a.started);
  if ((x = p.match(/^\/api\/conversations\/(\d+)$/))) {
    if (m === "DELETE") {
      d.convs = d.convs.filter(c => c.id !== +x![1]);
      return ok;
    }
    return withItems(d.convs.find(c => c.id === +x![1])!);
  }
  if (p === "/api/items")
    return clone(d.items)
      .filter(i => !url.searchParams.get("unseen") || !i.seen)
      .sort((a, b) => b.ts - a.ts);
  if ((x = p.match(/^\/api\/items\/(\d+)\/seen$/))) {
    d.items.find(i => i.id === +x![1])!.seen = true;
    return ok;
  }
  if ((x = p.match(/^\/api\/items\/(\d+)\/entries\/(\d+)$/))) {
    d.items.find(i => i.id === +x![1])!.entries![+x[2]].done = b.done;
    return ok;
  }
  if ((x = p.match(/^\/api\/items\/(\d+)$/)) && m === "DELETE") {
    d.items = d.items.filter(i => i.id !== +x![1]);
    return ok;
  }
  if ((x = p.match(/^\/api\/turns\/(\d+)\/(rating|correction)$/))) {
    const t = d.convs.flatMap(c => c.turns).find(t => t.id === +x![1])!;
    if (x[2] === "rating") t.rating = b.rating;
    else t.corrected_text = b.text === t.text ? null : b.text;
    return ok;
  }
  if ((x = p.match(/^\/api\/events\/(\d+)\/label$/))) {
    d.events.find(e => e.id === +x![1])!.label = b.label;
    return ok;
  }
  if ((x = p.match(/^\/api\/events\/(\d+)\/cluster$/))) {
    Object.assign(
      d.events.find(e => e.id === +x![1])!,
      { cluster_id: b.cluster_id, cluster_pinned: 1 },
    );
    return ok;
  }
  if ((x = p.match(/^\/api\/events\/(\d+)$/)) && m === "DELETE") {
    d.events = d.events.filter(e => e.id !== +x![1]);
    return ok;
  }
  if (p === "/api/clusters" && m === "POST") {
    const id = Math.max(0, ...d.clusters.map(c => c.id)) + 1;
    d.clusters.push({ id, name: b.name, kind: b.name ? "person" : "unknown" });
    return { id };
  }
  if (p === "/api/clusters/merge") {
    d.events.forEach(e => {
      if (e.cluster_id === b.absorb) Object.assign(e, { cluster_id: b.keep, cluster_pinned: 1 });
    });
    d.clusters = d.clusters.filter(c => c.id !== b.absorb);
    return ok;
  }
  if ((x = p.match(/^\/api\/clusters\/(\d+)$/))) {
    const c = d.clusters.find(c => c.id === +x![1])!;
    Object.assign(c, {
      name: b.name,
      kind: b.kind ?? (c.kind === "not_person" ? "not_person" : b.name ? "person" : "unknown"),
    });
    d.convs.forEach(c => {
      if (c.speaker?.cluster_id === +x![1]) c.speaker.name = b.name;
    });
    return ok;
  }
  if (p === "/api/recluster") {
    await sleep(1500);
    return { summary: MOCK_TOASTS.recluster };
  }
  if (p === "/api/retrain") {
    await sleep(2500);
    const r = (d.models.last_retrain = { ...MOCK_RETRAIN.better, ts: Date.now() / 1000 });
    d.models.history = [
      { version: r.version!, ts: r.ts!, active: true, note: "Retrained on 48 of your wakes (41 real, 7 not)." },
      ...d.models.history.map(h => ({ ...h, active: false })),
    ];
    return r;
  }
  if (p === "/api/models/use") {
    d.models.history = d.models.history.map(h => ({ ...h, active: h.version === b.version }));
    return ok;
  }
  throw new Error("Not Found");
}
