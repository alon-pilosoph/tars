/* Demo data for ?demo: a sample household, so every state can be opened from a link. */
import type { Opts } from "./api";
import { DEMO_LINK } from "./demoParams";
import { personName } from "./format";
import type {
  AnsweredBy,
  Cluster,
  Conversation,
  ConversationWake,
  FailedAt,
  Item,
  Label,
  Metric,
  ModelsInfo,
  Outcome,
  Reminder,
  RemindersInfo,
  Speaker,
  TarsEvent,
  Turn,
} from "./types";

type DemoEvent = Omit<TarsEvent, "auto_label" | "auto_reason">;
type DemoTurn = Omit<Turn, "items"> & { items?: number[] };
type DemoConv = Omit<Conversation, "turns"> & { turns: DemoTurn[] };
type DemoCluster = Omit<Cluster, "size" | "samples">;

interface DemoData {
  events: DemoEvent[];
  clusters: DemoCluster[];
  convs: DemoConv[];
  items: Item[];
  models: ModelsInfo;
  reminders: RemindersInfo;
}
let D: DemoData | null = null;

/** What TARS says for a reminder; mirrors reminders.line. */
function says(r: Pick<Reminder, "kind" | "text" | "for_name" | "from_name">) {
  const name = personName;
  const who = r.for_name ? `${name(r.for_name)}, ` : "";
  const sender = r.from_name && r.from_name.toLowerCase() !== r.for_name?.toLowerCase() ? r.from_name : null;
  if (r.kind === "timer") {
    const label = r.text ? `${r.text} timer` : "timer";
    return who ? `${who}your ${label} is done.` : `Your ${label} is done.`;
  }
  const what = r.kind === "message" ? "a message" : "a reminder";
  const head =
    (who ? `${who}${what}` : what[0].toUpperCase() + what.slice(1)) + (sender ? ` from ${name(sender)}` : "");
  const text = r.text || "";
  return `${head}: ${/[.!?]$/.test(text) ? text : `${text}.`}`;
}

type ReminderRow = Partial<Reminder> & Pick<Reminder, "id" | "kind" | "status">;
function reminder(r: ReminderRow, now: number): Reminder {
  const full: Reminder = {
    created: now - 3600,
    text: null,
    for_name: null,
    from_name: null,
    set_via: "web",
    conversation_id: null,
    due: now,
    needs_ack: true,
    repeat_every_s: r.kind === "timer" ? 10 : 300,
    max_tries: r.kind === "timer" ? 90 : 4,
    tries: 0,
    next_at: null,
    last_said: null,
    acked_at: null,
    acked_by: null,
    acked_via: null,
    says: "",
    ...r,
  };
  return { ...full, says: says(full) };
}

const MISSED_WINDOW_S = 6; // mirrors events.py

interface WakeRow {
  minutesAgo: number;
  kind?: TarsEvent["kind"];
  outcome?: Outcome;
  score: number;
  heard?: string;
  confidence?: number;
  transcript?: string | null;
  follow?: TarsEvent["follow"];
  cluster?: number;
  pinned?: boolean;
  label?: Label;
}

const WAKES: WakeRow[] = [
  {
    minutesAgo: 400,
    outcome: "answer",
    score: 0.92,
    heard: "hey tars",
    confidence: 0.97,
    transcript: "What's the weather going to be like tomorrow?",
    follow: "asked",
    cluster: 1,
  },
  {
    minutesAgo: 355,
    outcome: "answer",
    score: 0.92,
    heard: "hey tars",
    confidence: 0.97,
    transcript: "Set a timer for twelve minutes.",
    follow: "asked",
    cluster: 1,
  },
  {
    minutesAgo: 330,
    outcome: "answer",
    score: 0.88,
    heard: "hey tars",
    confidence: 0.9,
    transcript: "Play some jazz.",
    follow: "asked",
    cluster: 2,
  },
  {
    minutesAgo: 310,
    outcome: "answer",
    score: 0.92,
    heard: "hey tars",
    confidence: 0.97,
    transcript: "Remind me to call Stacey after dinner.",
    follow: "asked",
    cluster: 1,
  },
  {
    minutesAgo: 280,
    outcome: "answer",
    score: 0.88,
    heard: "hey tars",
    confidence: 0.9,
    transcript: "Add eggs, milk and coffee to the shopping list.",
    follow: "asked",
    cluster: 2,
  },
  {
    minutesAgo: 265,
    outcome: "answer",
    score: 0.92,
    heard: "hey tars",
    confidence: 0.97,
    transcript: "Send me everything for the Galilee trip.",
    follow: "asked",
    cluster: 1,
  },
  {
    minutesAgo: 250,
    outcome: "answer",
    score: 0.81,
    heard: "hey darts",
    confidence: 0.74,
    transcript: "Is it going to rain?",
    follow: "asked",
    cluster: 3,
  },
  {
    minutesAgo: 230,
    outcome: "answer",
    score: 0.88,
    heard: "hey tars",
    confidence: 0.9,
    transcript: "Can you send me the lasagna recipe from last Sunday?",
    follow: "asked",
    cluster: 2,
  },
  {
    minutesAgo: 219,
    outcome: "answer",
    score: 0.81,
    heard: "hey darts",
    confidence: 0.74,
    transcript: "What time is it in Tokyo?",
    follow: "asked",
    cluster: 3,
  },
  {
    minutesAgo: 200,
    outcome: "answer",
    score: 0.63,
    heard: "hey tars",
    confidence: 0.41,
    transcript: "and in tonight's top story the storm is moving east",
    follow: "not_for_us",
    cluster: 4,
  },
  {
    minutesAgo: 180,
    outcome: "answer",
    score: 0.63,
    heard: "hey tars",
    confidence: 0.41,
    transcript: "you won't believe what happened next",
    follow: "not_for_us",
    cluster: 5,
  },
  { minutesAgo: 150, outcome: "answer", score: 0.86, heard: "hey tars", confidence: 0.93, follow: "said_nothing" },
  { minutesAgo: 100, outcome: "ask", score: 0.66, heard: "hey bars", confidence: 0.12, follow: "said_nothing" },
  {
    minutesAgo: 90,
    outcome: "ask",
    score: 0.64,
    heard: "hey mars",
    confidence: 0.1,
    transcript: "No.",
    follow: "not_for_us",
    cluster: 1,
  },
  { minutesAgo: 80, outcome: "ignore", score: 0.58, heard: "hey stars", confidence: 0.04 },
  { minutesAgo: 70, outcome: "ignore", score: 0.55, heard: "[unk] stars", confidence: 0.02 },
  { minutesAgo: 60, outcome: "ignore", score: 0.73, heard: "bars", confidence: 0.16, label: "real" },
  { minutesAgo: 40, kind: "near_miss", score: 0.41 },
  {
    minutesAgo: 39.95,
    outcome: "answer",
    score: 0.9,
    heard: "hey tars",
    confidence: 0.95,
    transcript: "Turn off the lights.",
    follow: "asked",
    cluster: 1,
    pinned: true,
  },
  { minutesAgo: 12, kind: "near_miss", score: 0.36 },
];

const CAMPSITE_MAP = [
  "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360'><defs>",
  "<pattern id='p' width='14' height='14' patternUnits='userSpaceOnUse' patternTransform='rotate(45)'>",
  "<rect width='14' height='14' fill='#d8d5ce'/><rect width='7' height='14' fill='#cdc9c1'/></pattern></defs>",
  "<rect width='640' height='360' fill='url(#p)'/>",
  "<text x='320' y='186' font-family='monospace' font-size='18' text-anchor='middle' fill='#5f5c56'>",
  "campsite map (photo)</text></svg>",
].join("");

const LASAGNA = [
  "**Serves 6, about 1 h 20 min**",
  "",
  "# You need",
  "- 12 lasagna sheets",
  "- 500 g ricotta",
  "- 400 g spinach",
  "- 2 jars of tomato sauce",
  "- 250 g mozzarella",
  "",
  "# Method",
  "1. Oven to 190°C.",
  "2. Wilt the spinach, squeeze it dry, mix it into the ricotta.",
  "3. Layer sauce, sheets, ricotta. Repeat three times.",
  "4. Mozzarella on top. 45 min covered, 15 uncovered.",
  "",
  "Let it rest 10 minutes. *It's better the next day.*",
].join("\n");

const DIRECTIONS = [
  "About **2 h 40 min** from home.",
  "",
  "1. Route 6 north to the end.",
  "2. Route 90 through Rosh Pina.",
  "3. Right at the Kfar Blum junction, then follow the brown signs.",
  "",
  "Last fuel is at Rosh Pina.",
].join("\n");

const demoFile = () =>
  URL.createObjectURL(new Blob(["A file from the TARS demo. There's no real one."], { type: "text/plain" }));

const ALON: Speaker = { cluster_id: 1, name: "Alon" };
const STACEY: Speaker = { cluster_id: 2, name: "Stacey" };
const VOICE_3: Speaker = { cluster_id: 3, name: null };

function build(): DemoData {
  const now = Date.now() / 1000;
  const empty = DEMO_LINK.data === "empty";
  const ago = (minutes: number) => now - minutes * 60;
  const yesterday = (hour: number, minute: number) => {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    d.setHours(hour, minute, 0, 0);
    return d.getTime() / 1000;
  };

  const events: DemoEvent[] = empty
    ? []
    : WAKES.map((w, i) => ({
        id: i + 1,
        ts: ago(w.minutesAgo),
        kind: w.kind ?? "wake",
        outcome: w.outcome ?? null,
        wake_score: w.score,
        heard: w.heard ?? null,
        confidence: w.confidence ?? null,
        transcript: w.transcript ?? null,
        follow: w.follow ?? null,
        cluster_id: w.cluster ?? null,
        cluster_pinned: !!w.pinned,
        label: w.label ?? null,
        has_wake_audio: true,
        has_request_audio: !!w.transcript,
      }));
  if (DEMO_LINK.reviewDone)
    events.forEach(e => {
      if (e.follow === "asked" || e.label) return;
      e.label = e.follow === "not_for_us" || e.outcome === "ignore" ? "not_real" : "real";
    });

  const clusters: DemoCluster[] = empty
    ? []
    : [
        { id: 1, name: "Alon", kind: "person" },
        { id: 2, name: "Stacey", kind: "person" },
        { id: 3, name: null, kind: "unknown" },
        { id: 4, name: null, kind: "not_person" },
        { id: 5, name: null, kind: "unknown" },
      ];

  const items: Item[] = [
    {
      id: 101,
      kind: "note",
      title: "Lasagna (the one from last Sunday)",
      scope: "person",
      for: STACEY,
      seen: false,
      conversation_id: 1,
      ts: ago(20),
      body: LASAGNA,
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
      ts: ago(210),
      body: DIRECTIONS,
    },
    {
      id: 106,
      kind: "list",
      title: "Galilee packing list",
      scope: "person",
      for: ALON,
      seen: true,
      conversation_id: 7,
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
      ].map((text, n) => ({ text, done: n < 2 })),
    },
    {
      id: 107,
      kind: "file",
      title: "Campsite map",
      name: "kfar-blum-map.jpg",
      mime: "image/jpeg",
      size: 1258291,
      url: "data:image/svg+xml," + encodeURIComponent(CAMPSITE_MAP),
      preview: "data:image/svg+xml," + encodeURIComponent(CAMPSITE_MAP),
      scope: "person",
      for: ALON,
      seen: false,
      conversation_id: 7,
      ts: ago(210),
    },
    {
      id: 108,
      kind: "file",
      title: "Dishwasher warranty",
      name: "dishwasher-warranty.pdf",
      mime: "application/pdf",
      size: 356352,
      url: demoFile(),
      scope: "person",
      for: STACEY,
      seen: true,
      conversation_id: 8,
      ts: yesterday(21, 3),
    },
  ];
  if (DEMO_LINK.seenAll)
    items.forEach(i => {
      i.seen = true;
    });

  // [who: "P" (person) or "T" (TARS), what was said, extras]
  // took: seconds to first sound; by: who wrote it; failed: where answering failed, and why
  type TurnExtra = {
    rating?: "good" | "bad";
    items?: number[];
    speaker?: Speaker;
    fix?: string;
    aside?: boolean;
    took?: number;
    by?: AnsweredBy;
    failed?: [FailedAt, string];
  };
  type TurnRow = [string, string, TurnExtra?];
  const conv = (id: number, started: number, speaker: Speaker, wake: ConversationWake, turns: TurnRow[]): DemoConv => ({
    id,
    started,
    speaker,
    wake,
    turns: turns.map(([who, text, extra], n) => ({
      id: id * 10 + n + 1,
      ts: started + n * 8,
      role: who === "T" ? "tars" : "person",
      text,
      ...(who === "T"
        ? {
            rating: extra?.rating ?? null,
            items: extra?.items || [],
            timings: extra?.took != null ? timings(extra.took) : null,
            answered_by: extra?.by ?? null,
            failed_at: extra?.failed?.[0] ?? null,
            error: extra?.failed?.[1] ?? null,
          }
        : {
            has_audio: true,
            speaker: extra?.speaker ?? speaker,
            corrected_text: extra?.fix ?? null,
            not_for_tars: !!extra?.aside,
          }),
    })),
  });
  // A plausible split of `took` seconds: a pause, a quick transcript, then the brain and the voice.
  const timings = (took: number) => {
    const [waited, stt] = [0.25, 0.06];
    const llm = +((took - waited - stt) * 0.55).toFixed(2);
    return { end_of_speech: waited, stt, llm, tts: +(took - waited - stt - llm).toFixed(2), total: took };
  };
  // A conversation's wake isn't one of the wakes above (they'd change the voices' counts): ids from 1000 up.
  const wake = (convId: number, speaker: Speaker, heard: string, confidence: number, outcome: Outcome = "answer") => ({
    event_id: 1000 + convId,
    heard,
    confidence,
    outcome,
    label: null,
    cluster_id: speaker.cluster_id,
    has_request_audio: true,
  });
  const SENT = "Sent it. It's on the TARS page.";
  const convs: DemoConv[] = empty
    ? []
    : [
        conv(1, ago(21), STACEY, wake(1, STACEY, "hey tars", 0.96), [
          ["P", "Can you send me the lasagna recipe from last Sunday?"],
          ["T", SENT, { items: [101], took: 3.0, by: "look_up" }],
        ]),
        conv(2, ago(52), ALON, wake(2, ALON, "hey tars", 0.97), [
          ["P", "What's the weather tomorrow morning?"],
          ["T", "Cloudy, 14 degrees. Rain from about four.", { took: 2.9, by: "look_up" }],
          ["P", "Should I take the bike?"],
          ["T", "I'd take the bus. The rain is heaviest around six.", { took: 1.3, by: "quick" }],
          ["P", "When's the next 18 from Herzl?"],
          ["T", "7:52, then 8:07.", { took: 3.1, by: "look_up" }],
          ["P", "Send me the timetable."],
          ["T", SENT, { items: [102], took: 3.4, by: "look_up" }],
        ]),
        conv(3, ago(82), STACEY, wake(3, STACEY, "hey tars", 0.94), [
          ["P", "Add eggs, milk and toffee to the shopping list."],
          ["T", "Added eggs, milk and toffee. The list has seven things.", { items: [103], took: 2.6, by: "look_up" }],
        ]),
        conv(4, ago(105), ALON, wake(4, ALON, "hey tars", 0.95), [
          ["P", "Set a timer for twelve minutes."],
          ["T", "Twelve minutes, starting now.", { took: 2.4, by: "quick" }],
          ["P", "Stacey, can you check the oven?", { aside: true }],
          ["P", "It's fine, leave it.", { aside: true, speaker: STACEY }],
        ]),
        conv(5, ago(130), ALON, wake(5, ALON, "hey cars", 0.18, "ask"), [
          ["T", "Did you call me?"],
          ["P", "Yes, set an alarm for seven."],
          ["T", "Alarm set for 7:00 tomorrow morning.", { took: 2.5, by: "quick" }],
        ]),
        conv(6, ago(160), VOICE_3, wake(6, VOICE_3, "hey darts", 0.74), [
          ["P", "What time is it in Tokyo?"],
          ["T", "12:40 at night. Tomorrow already, over there.", { took: 1.1, by: "quick" }],
        ]),
        conv(7, ago(212), ALON, wake(7, ALON, "hey tars", 0.97), [
          ["P", "Send me everything for the Galilee trip."],
          [
            "T",
            "Sent four things: the booking, directions, the packing list and the campsite map. They're on the TARS " +
              "page.",
            { items: [104, 105, 106, 107], took: 4.2, by: "look_up" },
          ],
        ]),
        conv(8, yesterday(21, 2), STACEY, wake(8, STACEY, "hey tars", 0.95), [
          ["P", "Send me the warranty for the dishwasher."],
          ["T", "Sent it. It's a PDF, on the TARS page.", { items: [108], took: 2.7, by: "look_up" }],
        ]),
        conv(9, yesterday(20, 15), ALON, wake(9, ALON, "hey tars", 0.97), [
          ["P", "Tell me something about black holes."],
          [
            "T",
            "If you fell into one, you'd see the whole future of the universe flash past. Probably. Nobody's come back " +
              "to confirm.",
            { rating: "good", took: 1.5, by: "quick" },
          ],
        ]),
        conv(10, yesterday(19, 40), ALON, wake(10, ALON, "hey tars", 0.97), [
          ["P", "Remind me to call Tracy after dinner.", { fix: "Remind me to call Stacey after dinner." }],
          ["T", "I'll remind you to call Tracy at 8:30.", { rating: "bad", took: 1.3, by: "quick" }],
        ]),
        conv(11, yesterday(18, 0), STACEY, wake(11, STACEY, "hey tars", 0.9), [
          ["P", "Is it going to rain?"],
          ["T", "Not today. Clear until Thursday.", { took: 2.2, by: "fallback" }],
        ]),
        conv(12, yesterday(17, 10), ALON, wake(12, ALON, "hey tars", 0.96), [
          ["P", "What's on at the cinema tonight?"],
          ["T", "Looking it up.", { took: 0.9, by: "look_up", failed: ["llm", "APITimeoutError: Request timed out."] }],
        ]),
      ];

  const tomorrowAt9 = yesterday(9, 0) + 2 * 86400;
  const reminders: Reminder[] = (
    empty
      ? []
      : ([
          {
            id: 7,
            kind: "message",
            status: "waiting",
            text: "the plumber is coming at four",
            for_name: "stacey",
            from_name: "alon",
            due: ago(3),
            tries: 2,
            last_said: ago(1),
            next_at: ago(-4),
          },
          {
            id: 10,
            kind: "timer",
            status: "waiting",
            text: "eggs",
            set_via: "voice",
            due: ago(1),
            tries: 6,
            last_said: ago(0.1),
            next_at: ago(-0.1),
          },
          {
            id: 8,
            kind: "timer",
            status: "scheduled",
            text: "pasta",
            set_via: "voice",
            due: ago(-7),
            next_at: ago(-7),
          },
          {
            id: 6,
            kind: "message",
            status: "scheduled",
            text: "the parcel is at the Cohens'",
            for_name: "stacey",
            from_name: "alon",
            set_via: "voice",
            conversation_id: 2,
            due: null,
          },
          {
            id: 9,
            kind: "reminder",
            status: "scheduled",
            text: "call the bank about the mortgage",
            for_name: "alon",
            due: tomorrowAt9,
            next_at: tomorrowAt9,
          },
          {
            id: 4,
            kind: "timer",
            status: "acknowledged",
            set_via: "voice",
            conversation_id: 4,
            due: ago(93),
            tries: 1,
            last_said: ago(93),
            acked_at: ago(92),
            acked_by: "alon",
            acked_via: "voice",
          },
          {
            id: 5,
            kind: "reminder",
            status: "missed",
            text: "take the bins out",
            due: ago(140),
            tries: 4,
            last_said: ago(122),
          },
          {
            id: 3,
            kind: "message",
            status: "acknowledged",
            text: "pick up oat milk on the way",
            for_name: "alon",
            from_name: "stacey",
            due: ago(200),
            tries: 2,
            last_said: ago(198),
            acked_at: ago(190),
            acked_via: "web",
          },
          {
            id: 2,
            kind: "reminder",
            status: "said",
            text: "the dishwasher is done",
            needs_ack: false,
            due: yesterday(21, 30),
            tries: 1,
            last_said: yesterday(21, 30),
          },
          { id: 1, kind: "timer", status: "cancelled", text: "tea", set_via: "voice", due: yesterday(16, 0) },
        ] as ReminderRow[])
  ).map(r => reminder(r, now));

  const pair = "voice_data/events/wake_models/generic/v2";
  return {
    events,
    clusters,
    convs,
    items: empty ? [] : items,
    reminders: {
      enabled: !DEMO_LINK.remindersOff,
      reminders,
      voices: ["alon", "stacey"],
      defaults: { repeat_every_min: 5, max_tries: 4, timer_ring_min: 15 },
    },
    models: {
      active: empty
        ? {
            version: "installed",
            wake_model: "models/generic/hey_tars.tflite",
            threshold: 0.5,
            check_model: "models/generic/hey_tars_check.json",
            check_window_s: 3.0,
            replaced: null,
          }
        : {
            version: "v2",
            wake_model: `${pair}/hey_tars.tflite`,
            threshold: 0.5,
            check_model: `${pair}/hey_tars_check.json`,
            check_window_s: 3.0,
            replaced: "v1",
          },
      results: empty ? null : MOCK_RESULTS,
      history: [
        ...(empty
          ? []
          : [
              {
                version: "v2",
                ts: now - 3600 * 5,
                active: true,
                note: "Trained on 43 of your wakes (37 real, 6 not) and 5 missed ones.",
              },
              {
                version: "v1",
                ts: now - 3600 * 30,
                active: false,
                note: "Trained on 21 of your wakes (18 real, 3 not) and 2 missed ones.",
              },
            ]),
        { version: "installed", ts: now - 3600 * 24 * 12, active: empty, note: "What TARS was installed with." },
      ],
      problem: null,
      learning: empty
        ? { real: 0, not_real: 0, missed: 0, to_review: 0 }
        : { real: 9, not_real: 2, missed: 3, to_review: 4 },
    },
  };
}

/** Mirrors the server's auto_label (events.py). */
function demoAuto(e: DemoEvent, answered: number[]): [Label | null, string] {
  if (e.kind === "near_miss") {
    const missed = answered.some(t => t - e.ts > 0 && t - e.ts <= MISSED_WINDOW_S);
    return missed
      ? ["real", "a real wake followed within seconds, so it was probably a missed hey TARS"]
      : [null, "it never woke up"];
  }
  if (e.outcome === "ignore") return ["not_real", "the double-check heard something else"];
  if (e.follow === "asked") {
    return ["real", e.outcome === "answer" ? "a request followed" : "they answered “Did you call me?”"];
  }
  if (e.follow === "not_for_us") return ["not_real", "the reply wasn't meant for TARS"];
  if (e.follow === "said_nothing") {
    return e.outcome === "ask"
      ? ["not_real", "nobody answered “Did you call me?”"]
      : [null, "it woke, but nobody spoke"];
  }
  return [null, ""];
}

/** ?demo=long: the same household with everything long, to see what wraps, clips or overflows. */
function stress(d: DemoData) {
  const LONG_A = "Maximiliano Alessandro";
  const LONG_S = "Anastasia-Konstantina";
  const longName = (name: string | null) => (name === "Alon" ? LONG_A : name === "Stacey" ? LONG_S : name);
  const rename = (sp: Speaker | null | undefined) => {
    if (sp) sp.name = longName(sp.name);
  };
  d.clusters.forEach(c => {
    c.name = longName(c.name);
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
  const now = Date.now() / 1000;
  const url =
    "https://www.example.org/recipes/2026/09/the-absolutely-definitive-guide-to-slow-cooked-lasagna-with-" +
    "spinach-and-ricotta?utm_source=tars&ref=kitchen";
  const long =
    "Could you please remind me tomorrow morning, before I leave for work but after I've had my coffee, to " +
    "call the plumber about the dripping kitchen tap, and also to buy more of that oat milk Stacey likes, and to " +
    "check whether the recycling goes out on Thursday or Friday this week?";
  const reply =
    "Tomorrow at 7:40, between coffee and leaving: call the plumber about the kitchen tap. I've added oat " +
    "milk to the shopping list. Recycling goes out on Thursday this week, because Friday is a public holiday, which " +
    "the council mentioned in a letter nobody read. I'd put a sticky note on the door as well. Belt and braces.";
  const alon = { cluster_id: 1, name: LONG_A };
  const sent = { conversation_id: 12, seen: false, ts: now - 60 };
  d.items.push(
    {
      id: 201,
      kind: "link",
      title:
        "The absolutely definitive guide to slow-cooked lasagna with spinach, ricotta and " +
        "three kinds of cheese, tested forty times",
      site: "a-very-long-food-blog-name.example.org",
      url,
      description:
        "Everything you need, including the sauce, the resting time and why you should never skip it. " +
        `Found at ${url}`,
      scope: "person",
      for: alon,
      ...sent,
    },
    {
      id: 202,
      kind: "list",
      title: "Everything for the big family barbecue on Saturday afternoon at the park by the river",
      scope: "household",
      for: null,
      ...sent,
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
      url: demoFile(),
      scope: "person",
      for: { cluster_id: 2, name: LONG_S },
      ...sent,
    },
    {
      id: 204,
      kind: "note",
      title: "Supercalifragilisticexpialidocious-and-other-very-long-words-that-never-break",
      scope: "person",
      for: alon,
      ...sent,
      body:
        "# A long heading that keeps going and going well past the width of the card\n\n" +
        "Pneumonoultramicroscopicsilicovolcanoconiosis ".repeat(3) +
        "\n\n" +
        Array.from(
          { length: 12 },
          (_, k) => `- Step ${k + 1}: do the thing carefully, then check it twice before ` + "moving on",
        ).join("\n"),
    },
  );
  const heard = "hey tars can you hear me over the dishwasher";
  d.convs.unshift({
    id: 12,
    started: now - 90,
    speaker: alon,
    wake: {
      event_id: 99,
      heard,
      confidence: 0.61,
      outcome: "answer",
      label: null,
      cluster_id: 6,
      has_request_audio: true,
    },
    turns: [
      {
        id: 121,
        ts: now - 90,
        role: "person",
        text: long,
        has_audio: true,
        speaker: alon,
        corrected_text: null,
        not_for_tars: false,
      },
      { id: 122, ts: now - 80, role: "tars", text: reply, rating: null, items: [201, 202, 203, 204] },
      {
        id: 123,
        ts: now - 70,
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
    ts: now - 100,
    kind: "wake",
    outcome: "ask",
    wake_score: 0.7,
    heard,
    confidence: 0.3,
    transcript: long,
    follow: "not_for_us",
    cluster_id: 6,
    cluster_pinned: true,
    label: null,
    has_wake_audio: true,
    has_request_audio: true,
  });
  if (d.models.active) {
    d.models.active.wake_model =
      "models/generic/experiments/2026-09-26/" + "hey_tars_v3real_accent_long_window_final.tflite";
  }
}

export const MOCK_TOASTS = {
  recluster:
    "Grouped 7 requests into 3 voices, 1 of them new. 2 requests moved. Anything you set by hand stayed put. " +
    "Updated the voiceprints of Alon.",
  renamed: "Saved. 2 more requests for Stacey's voiceprint.",
};

const metric = (name: string, current: string, candidate: string, lowerIsBetter = false): Metric => ({
  name,
  current,
  candidate,
  lower_is_better: lowerIsBetter,
});
const MOCK_RESULTS: Metric[] = [
  metric("Your held-out hey TARS", "9 of 12", "11 of 12"),
  metric("Your held-out wakes that weren't for TARS, let through", "1 of 3", "0 of 3", true),
  metric("Other voices, quiet", "94.4%", "95.6%"),
  metric("Other voices, TV and chatter", "80.6%", "80.8%"),
  metric("Lookalikes let through", "1.8%", "1.8%", true),
  metric("False answers per hour, TV", "0.0", "0.0", true),
  metric("False answers per hour, audiobooks", "0.0", "0.0", true),
];

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));

function data() {
  if (!D) {
    D = build();
    if (DEMO_LINK.data === "long") stress(D);
  }
  return D;
}

export async function demoApi(path: string, opts: Opts = {}): Promise<unknown> {
  const d = data();
  await sleep(60);
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : {};
  const ok = { ok: true };
  const newestFirst = <T extends { ts: number }>(xs: T[]) => [...xs].sort((a, b) => b.ts - a.ts);
  const event = (id: number) => d.events.find(e => e.id === id);
  const convWake = (id: number) => d.convs.find(c => c.wake?.event_id === id)?.wake;
  const item = (id: number) => d.items.find(i => i.id === id);
  const withItems = (c: DemoConv) => ({
    ...clone(c),
    turns: c.turns.map(t => ({ ...clone(t), items: (t.items || []).filter(id => item(id)) })),
  });
  const p = new URL(path, "http://demo").pathname;
  let m: RegExpMatchArray | null;

  if (p === "/api/events") {
    const answered = d.events.filter(e => e.kind === "wake" && e.outcome === "answer").map(e => e.ts);
    return newestFirst(d.events).map(e => {
      const [auto_label, auto_reason] = demoAuto(e, answered);
      return { ...e, auto_label, auto_reason };
    });
  }
  if (p === "/api/clusters" && method === "GET") {
    return d.clusters
      .map(c => {
        const own = newestFirst(d.events.filter(e => e.cluster_id === c.id));
        const samples = own
          .filter(e => e.has_request_audio)
          .slice(0, 4)
          .map(e => ({ event_id: e.id, transcript: e.transcript }));
        return { ...c, size: own.length, samples };
      })
      .sort((a, b) => b.size - a.size);
  }
  if (p === "/api/models") return d.models;
  if (p === "/api/status") return { clustering: true, enroll_at: 5 };
  if (p === "/api/conversations") return d.convs.map(withItems).sort((a, b) => b.started - a.started);
  if ((m = p.match(/^\/api\/conversations\/(\d+)$/))) {
    const id = Number(m[1]);
    if (method === "DELETE") {
      d.convs = d.convs.filter(c => c.id !== id);
      return ok;
    }
    const c = d.convs.find(x => x.id === id);
    if (c) return withItems(c);
  }
  if (p === "/api/items") return newestFirst(clone(d.items));
  if ((m = p.match(/^\/api\/items\/(\d+)\/seen$/))) {
    const i = item(Number(m[1]));
    if (i) i.seen = true;
    return ok;
  }
  if ((m = p.match(/^\/api\/items\/(\d+)\/entries\/(\d+)$/))) {
    const entry = item(Number(m[1]))?.entries?.[Number(m[2])];
    if (entry) entry.done = body.done;
    return ok;
  }
  if ((m = p.match(/^\/api\/items\/(\d+)$/)) && method === "DELETE") {
    const id = Number(m[1]);
    d.items = d.items.filter(i => i.id !== id);
    return ok;
  }
  if ((m = p.match(/^\/api\/turns\/(\d+)\/(rating|correction)$/))) {
    const id = Number(m[1]);
    const t = d.convs.flatMap(c => c.turns).find(x => x.id === id);
    if (t && m[2] === "rating") t.rating = body.rating;
    else if (t) t.corrected_text = body.text === t.text ? null : body.text;
    return ok;
  }
  if ((m = p.match(/^\/api\/events\/(\d+)\/label$/))) {
    const id = Number(m[1]);
    const target = event(id) ?? convWake(id);
    if (target) target.label = body.label;
    return ok;
  }
  if ((m = p.match(/^\/api\/events\/(\d+)\/cluster$/))) {
    const id = Number(m[1]);
    const e = event(id);
    if (e) Object.assign(e, { cluster_id: body.cluster_id, cluster_pinned: true });
    const w = convWake(id);
    if (w) w.cluster_id = body.cluster_id;
    return ok;
  }
  if ((m = p.match(/^\/api\/events\/(\d+)$/)) && method === "DELETE") {
    const id = Number(m[1]);
    d.events = d.events.filter(e => e.id !== id);
    return ok;
  }
  if (p === "/api/clusters" && method === "POST") {
    const id = Math.max(0, ...d.clusters.map(c => c.id)) + 1;
    d.clusters.push({ id, name: body.name, kind: body.name ? "person" : "unknown" });
    return { id };
  }
  if (p === "/api/clusters/merge") {
    d.events.forEach(e => {
      if (e.cluster_id === body.absorb) Object.assign(e, { cluster_id: body.keep, cluster_pinned: true });
    });
    d.clusters = d.clusters.filter(c => c.id !== body.absorb);
    return ok;
  }
  if ((m = p.match(/^\/api\/clusters\/(\d+)$/))) {
    const id = Number(m[1]);
    const c = d.clusters.find(x => x.id === id);
    const kept = c?.kind === "not_person" ? "not_person" : body.name ? "person" : "unknown";
    if (c) Object.assign(c, { name: body.name, kind: body.kind ?? kept });
    d.convs.forEach(conv => {
      if (conv.speaker?.cluster_id === id) conv.speaker.name = body.name;
    });
    return ok;
  }
  if (p === "/api/recluster") {
    await sleep(1500);
    return { summary: MOCK_TOASTS.recluster };
  }
  if (p === "/api/reminders" && method === "GET") {
    const active = (r: Reminder) => r.status === "scheduled" || r.status === "waiting";
    const order = (a: Reminder, b: Reminder) =>
      Number(active(b)) - Number(active(a)) ||
      (active(a) ? (a.next_at ?? 1e18) - (b.next_at ?? 1e18) : b.created - a.created);
    return { ...clone(d.reminders), reminders: clone(d.reminders.reminders).sort(order) };
  }
  if (p === "/api/reminders" && method === "POST") {
    if (body.kind === "message" && !body.for_name) throw new Error("a message needs someone it's for");
    const id = Math.max(0, ...d.reminders.reminders.map(r => r.id)) + 1;
    const now = Date.now() / 1000;
    const r = reminder(
      {
        id,
        kind: body.kind,
        status: "scheduled",
        text: body.text,
        for_name: body.for_name,
        from_name: body.from_name,
        due: body.due,
        next_at: body.due,
        needs_ack: body.needs_ack,
        repeat_every_s: body.kind === "timer" ? 10 : (body.repeat_every_min ?? 5) * 60,
        max_tries: body.kind === "timer" ? 90 : (body.max_tries ?? 4),
        created: now,
      },
      now,
    );
    d.reminders.reminders.push(r);
    return { id };
  }
  if ((m = p.match(/^\/api\/reminders\/(\d+)\/(ack|snooze|cancel)$/))) {
    const r = d.reminders.reminders.find(x => x.id === Number(m![1]));
    const now = Date.now() / 1000;
    if (r && m[2] === "ack")
      Object.assign(r, { status: "acknowledged", acked_at: now, acked_via: "web", next_at: null });
    if (r && m[2] === "snooze") Object.assign(r, { status: "scheduled", tries: 0, next_at: now + body.minutes * 60 });
    if (r && m[2] === "cancel") Object.assign(r, { status: "cancelled", next_at: null });
    return ok;
  }
  if (p === "/api/models/use") {
    d.models.history = d.models.history.map(h => ({ ...h, active: h.version === body.version }));
    if (d.models.active) d.models.active = { ...d.models.active, version: body.version };
    return ok;
  }
  throw new Error("Not Found");
}
