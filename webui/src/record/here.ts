/* Recording on the device the page is open on, for a wake test (recording.py's ON_DEVICE). The browser's
   microphone as the device gives it, its own echo cancelling and noise suppression left on, as a real device would
   have them; a take ends at a pause after speech, the way TARS ends one, or after a set time for the room's sound;
   and it's uploaded as a WAV at the device's own rate, which TARS converts. */

export type Stopped = "silence" | "tap" | "nobody";

const FRAME_S = 0.02;
// As recording.py's check: a frame this much louder than the room, and at least this loud, is speech.
const SPEECH_ABOVE_FLOOR_DB = 12;
const SPEECH_AT_LEAST_DB = -50;
const START_FRAMES = 3; // speech this many frames running has started
const END_SILENCE_S = 1.0; // then this much quiet ends the take
const NOBODY_S = 6; // nobody started speaking by now: nothing to keep
const LONGEST_S = 12;
const LEVEL_GAIN = 6; // as takes.py's: a frame's loudness times this is the meter's 0-1
const BEEP_HZ = 880;
const CHIME_HZ = 660; // a countdown's, lower than the beep that starts the take
const BEEP_S = 0.15;
const AFTER_BEEP_S = 0.25; // the beep has left the speaker before the take starts

export const NO_HTTPS =
  "This page can only use the microphone over HTTPS. Ask for a link that starts with https://, or open it on the " +
  "computer TARS runs on.";
export const BLOCKED = "The microphone is blocked for this page. Allow it in the browser's settings, then try again.";

/** Decides, a frame's loudness at a time, when a take is over: a pause after speech, nobody speaking, or too long. */
export class Endpointer {
  private dbs: number[] = [];
  private run = 0; // speech frames in a row
  private quiet = 0; // quiet frames in a row, once speech has started
  started = false;
  frames = 0;

  feed(db: number): Stopped | null {
    this.frames++;
    this.dbs.push(db);
    if (this.dbs.length > 500) this.dbs.shift();
    const sorted = [...this.dbs].sort((a, b) => a - b);
    const floor = sorted[Math.floor(sorted.length * 0.1)];
    const speech = db > Math.max(floor + SPEECH_ABOVE_FLOOR_DB, SPEECH_AT_LEAST_DB);
    this.run = speech ? this.run + 1 : 0;
    if (!this.started && this.run >= START_FRAMES) this.started = true;
    this.quiet = this.started && !speech ? this.quiet + 1 : 0;
    const seconds = this.frames * FRAME_S;
    if (this.started && this.quiet * FRAME_S >= END_SILENCE_S) return "silence";
    if (!this.started && seconds >= NOBODY_S) return "nobody";
    if (seconds >= LONGEST_S) return "silence";
    return null;
  }
}

export function wav(samples: Float32Array, rate: number): Blob {
  const out = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (at: number, s: string) => [...s].forEach((c, i) => out.setUint8(at + i, c.charCodeAt(0)));
  text(0, "RIFF");
  out.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVEfmt ");
  out.setUint32(16, 16, true);
  out.setUint16(20, 1, true); // PCM
  out.setUint16(22, 1, true); // mono
  out.setUint32(24, rate, true);
  out.setUint32(28, rate * 2, true);
  out.setUint16(32, 2, true);
  out.setUint16(34, 16, true);
  text(36, "data");
  out.setUint32(40, samples.length * 2, true);
  samples.forEach((x, i) => out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, x)) * 0x7fff, true));
  return new Blob([out.buffer], { type: "audio/wav" });
}

// Hands the microphone's samples to the page as they come.
const TAP = `registerProcessor("tars-tap", class extends AudioWorkletProcessor {
  process(inputs) { const ch = inputs[0] && inputs[0][0]; if (ch) this.port.postMessage(ch.slice(0)); return true; }
});`;

export interface Take {
  done: Promise<{ wav: Blob; stopped: Stopped }>;
  stop: () => void;
}

/** This device's microphone, asked for once (in a tap) and kept for the session. */
export class DeviceMic {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private listener: ((chunk: Float32Array) => void) | null = null;

  async open(): Promise<void> {
    if (this.ctx) return void (await this.ctx.resume());
    if (!navigator.mediaDevices?.getUserMedia) throw new Error(NO_HTTPS);
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
    } catch (e) {
      throw new Error((e as Error).name === "NotAllowedError" ? BLOCKED : (e as Error).message);
    }
    const ctx = new AudioContext();
    const url = URL.createObjectURL(new Blob([TAP], { type: "text/javascript" }));
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    const node = new AudioWorkletNode(ctx, "tars-tap");
    node.port.onmessage = e => this.listener?.(e.data as Float32Array);
    const silent = ctx.createGain();
    silent.gain.value = 0; // pulled through to the speaker so it runs, but never heard
    ctx.createMediaStreamSource(this.stream).connect(node).connect(silent).connect(ctx.destination);
    await ctx.resume();
    this.ctx = ctx;
    this.node = node;
  }

  /** A short beep from this device, then a moment for it to fade. */
  async beep(): Promise<void> {
    this.tone(BEEP_HZ);
    await new Promise(r => setTimeout(r, (BEEP_S + AFTER_BEEP_S) * 1000));
  }

  /** A countdown's chime: lower than the beep, and nothing waits for it. */
  chime() {
    this.tone(CHIME_HZ);
  }

  private tone(hz: number) {
    const ctx = this.ctx;
    if (!ctx) return;
    const tone = ctx.createOscillator();
    const gain = ctx.createGain();
    tone.frequency.value = hz;
    const t = ctx.currentTime;
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(0.3, t + 0.01);
    gain.gain.linearRampToValueAtTime(0, t + BEEP_S);
    tone.connect(gain).connect(ctx.destination);
    tone.start(t);
    tone.stop(t + BEEP_S);
  }

  /** Records until a pause after speech (or `seconds`, when set, whatever is said), or until stopped. */
  record(seconds: number, onLevel: (level: number, seconds: number) => void): Take {
    const ctx = this.ctx;
    if (!ctx || !this.node) throw new Error("The microphone isn't open.");
    const rate = ctx.sampleRate;
    const frame = Math.round(FRAME_S * rate);
    const chunks: Float32Array[] = [];
    let pending: number[] = [];
    let total = 0;
    const ends = new Endpointer();
    let over = false;
    let finish: (stopped: Stopped) => void = () => {};
    const done = new Promise<{ wav: Blob; stopped: Stopped }>(resolve => {
      finish = stopped => {
        if (over) return;
        over = true;
        this.listener = null;
        const all = new Float32Array(total);
        let at = 0;
        for (const c of chunks) all.set(c, (at += c.length) - c.length);
        resolve({ wav: wav(all, rate), stopped });
      };
    });
    this.listener = chunk => {
      chunks.push(chunk);
      total += chunk.length;
      pending = pending.concat(Array.from(chunk));
      while (pending.length >= frame) {
        const f = pending.splice(0, frame);
        const rms = Math.sqrt(f.reduce((sum, x) => sum + x * x, 0) / f.length);
        onLevel(Math.min(1, rms * LEVEL_GAIN), total / rate);
        if (seconds > 0) {
          if (total / rate >= seconds) return finish("silence");
        } else {
          const stopped = ends.feed(20 * Math.log10(rms + 1e-9));
          if (stopped) return finish(stopped);
        }
      }
    };
    return { done, stop: () => finish("tap") };
  }

  close() {
    this.listener = null;
    this.stream?.getTracks().forEach(t => t.stop());
    void this.ctx?.close();
    this.ctx = this.stream = this.node = null;
  }
}
