import { describe, expect, it } from "vitest";
import { type Events, type Line, type Mic, PageBody, type Speaker, type TalkState } from "./protocol";
import { downsampler, toFloat, toInt16 } from "./resample";

const sine = (hz: number, rate: number, seconds: number, level = 0.5) =>
  Float32Array.from({ length: Math.round(rate * seconds) }, (_, i) => level * Math.sin((2 * Math.PI * hz * i) / rate));

/** The strength of `hz` in `pcm` at 16 kHz (a single DFT bin), 0..1 of full scale. */
function strength(pcm: Int16Array, hz: number) {
  let re = 0,
    im = 0;
  for (let i = 0; i < pcm.length; i++) {
    const a = (2 * Math.PI * hz * i) / 16000;
    re += (pcm[i] / 0x8000) * Math.cos(a);
    im += (pcm[i] / 0x8000) * Math.sin(a);
  }
  return (2 * Math.hypot(re, im)) / pcm.length;
}

describe("the microphone, downsampled to 16 kHz", () => {
  it.each([48_000, 44_100, 16_000, 96_000])("from %i Hz: a second makes a second, a tone stays its pitch", rate => {
    const out = downsampler(rate)(sine(440, rate, 1));
    expect(Math.abs(out.length - 16000)).toBeLessThanOrEqual(1);
    expect(strength(out, 440)).toBeGreaterThan(0.45);
    expect(strength(out, 1000)).toBeLessThan(0.02);
  });

  it("keeps out most of what's above 8 kHz", () => {
    const out = downsampler(48_000)(sine(15_000, 48_000, 1));
    // 15 kHz would fold to 1 kHz; the averaging takes most of it out.
    expect(strength(out, 1000)).toBeLessThan(0.2);
  });

  it("gives the same samples whatever the chunks", () => {
    const input = sine(300, 44_100, 0.5);
    const whole = downsampler(44_100)(input);
    const down = downsampler(44_100);
    const pieces: number[] = [];
    for (let at = 0; at < input.length; at += 128) pieces.push(...down(input.subarray(at, at + 128)));
    expect(Int16Array.from(pieces)).toEqual(whole);
  });

  it("clips and converts without wrapping", () => {
    expect([toInt16(2), toInt16(-2), toInt16(0)]).toEqual([32767, -32768, 0]);
    expect(Array.from(toFloat(Int16Array.from([-32768, 0, 16384])))).toEqual([-1, 0, 0.5]);
  });
});

class FakeMic implements Mic {
  heard: ((pcm: Int16Array) => void) | null = null;
  refuse = false;
  async open(heard: (pcm: Int16Array) => void) {
    if (this.refuse) throw new DOMException("denied", "NotAllowedError");
    this.heard = heard;
  }
  close() {
    this.heard = null;
  }
}

class FakeSpeaker implements Speaker {
  got: number[] = [];
  ended: (() => void) | null = null;
  cuts = 0;
  start() {}
  audio(pcm: Int16Array) {
    this.got.push(pcm.length);
  }
  end(played: () => void) {
    this.ended = played;
  }
  cut() {
    this.cuts++;
    this.ended = null;
  }
}

function page() {
  const sent: (object | number)[] = [];
  const states: TalkState[] = [];
  const lines: Line[] = [];
  const refused: string[] = [];
  const mic = new FakeMic();
  const speaker = new FakeSpeaker();
  const events: Events = { state: s => states.push(s), line: l => lines.push(l), refused: w => refused.push(w) };
  const body = new PageBody(m => sent.push(typeof m === "string" ? JSON.parse(m) : m.byteLength), mic, speaker, events);
  const brain = (m: object) => body.brain(JSON.stringify(m));
  return { body, sent, states, lines, refused, mic, speaker, brain };
}

const speech = (n = 320) => new Int16Array(n).fill(1000);

describe("the Talk page's half of the body link", () => {
  it("says hello with its ticket, never a person", () => {
    const hello = JSON.parse(page().body.hello("t1"));
    expect(hello).toMatchObject({ type: "hello", protocol: 1, ticket: "t1", kind: "web", wake: "none" });
    expect(hello).not.toHaveProperty("person");
    expect(hello).not.toHaveProperty("device");
  });

  it("holds a button turn as the brain's fixture has it (tests/fixtures/bodylink/button-turn.json)", async () => {
    const t = page();
    t.brain({ type: "welcome", room: "Alon on the web 3f9a", play_rate: 24000, max_listen_s: 120 });
    t.brain({ type: "sleep" });
    expect(t.states).toEqual(["ready"]);
    await t.body.tap();
    expect(t.sent[0]).toEqual({ type: "wake", how: "button", score: 1, preroll_ms: 0 });
    t.mic.heard!(speech());
    expect(t.sent[1]).toBe(640); // the question, up as 16-bit audio
    t.brain({ type: "listen", on: false });
    t.mic.heard!(speech());
    expect(t.sent).toHaveLength(2); // not while the brain's not listening
    t.brain({ type: "text", who: "you", text: "What time is it?" });
    t.brain({ type: "play", id: 7, listening: false });
    t.body.brain(new Int16Array(2400).buffer);
    t.brain({ type: "text", who: "tars", text: "Nine." });
    t.brain({ type: "play_end", id: 7 });
    expect(t.speaker.got).toEqual([2400]);
    t.speaker.ended!();
    expect(t.sent[2]).toEqual({ type: "played", id: 7, cut: false });
    t.brain({ type: "listen", on: true });
    t.mic.heard!(speech());
    expect(t.sent[3]).toBe(640); // a follow-up
    t.brain({ type: "sleep" });
    expect(t.mic.heard).toBeNull(); // the microphone is closed between conversations
    expect(t.states).toEqual(["ready", "listening", "thinking", "speaking", "thinking", "listening", "ready"]);
    expect(t.lines).toEqual([
      { who: "you", text: "What time is it?" },
      { who: "tars", text: "Nine." },
    ]);
  });

  it("never streams from sleep, whatever the brain says, nor before it's let in", async () => {
    const t = page();
    await t.body.tap();
    expect(t.sent).toEqual([]); // not welcomed yet
    t.brain({ type: "welcome", play_rate: 24000, max_listen_s: 120 });
    await t.body.tap();
    expect(t.sent).toEqual([]); // let in, but the brain isn't waiting yet: a tap now would be dropped there
    t.brain({ type: "sleep" });
    t.brain({ type: "listen", on: true });
    expect(t.mic.heard).toBeNull();
    expect(t.sent).toEqual([]);
  });

  it("a tap while TARS speaks stops it, and says it was cut", async () => {
    const t = page();
    t.brain({ type: "welcome", play_rate: 24000, max_listen_s: 120 });
    t.brain({ type: "sleep" });
    await t.body.tap();
    t.brain({ type: "listen", on: false });
    t.brain({ type: "play", id: 3, listening: false });
    await t.body.tap();
    expect(t.sent.slice(-2)).toEqual([
      { type: "button", name: "stop" },
      { type: "played", id: 3, cut: true },
    ]);
    expect(t.speaker.cuts).toBeGreaterThan(0);
  });

  it("stops streaming at the conversation's cap", async () => {
    const t = page();
    t.brain({ type: "welcome", play_rate: 24000, max_listen_s: 1 });
    t.brain({ type: "sleep" });
    await t.body.tap();
    for (let i = 0; i < 60; i++) t.mic?.heard?.(speech());
    const audio = t.sent.filter(m => typeof m === "number") as number[];
    expect(audio.reduce((a, b) => a + b, 0) / 2).toBeLessThanOrEqual(16000);
    expect(t.sent).toContainEqual({ type: "gone_quiet" });
    expect(t.mic.heard).toBeNull();
  });

  it("a refused microphone says so, and the brain isn't left waiting", async () => {
    const t = page();
    t.brain({ type: "welcome", play_rate: 24000, max_listen_s: 120 });
    t.brain({ type: "sleep" });
    t.mic.refuse = true;
    await t.body.tap();
    expect(t.refused).toEqual(["NotAllowedError"]);
    expect(t.sent).toEqual([{ type: "wake", how: "button", score: 1, preroll_ms: 0 }, { type: "gone_quiet" }]);
    expect(t.body.state).toBe("ready");
  });

  it("a dropped connection ends whatever was going on", async () => {
    const t = page();
    t.brain({ type: "welcome", play_rate: 24000, max_listen_s: 120 });
    t.brain({ type: "sleep" });
    await t.body.tap();
    t.body.lost();
    expect(t.body.state).toBe("gone");
    expect(t.mic.heard).toBeNull();
  });
});
