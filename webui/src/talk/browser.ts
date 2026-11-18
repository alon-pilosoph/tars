/* The Talk page's microphone, speaker and connection, in a browser. One AudioContext for both, made (or resumed)
   in the tap that starts talking: phones only let a page make sound after a tap. */
import { post } from "../api";
import workletUrl from "./capture.worklet.ts?worker&url";
import { type Events, type Mic, PageBody, type Speaker } from "./protocol";
import { toFloat } from "./resample";

const LEAD_S = 0.06; // the first piece of a reply waits this long, so the next arrives before it ends

let shared: AudioContext | null = null;
/** The page's audio, running: call from a tap. */
export function audioContext(): AudioContext {
  shared ??= new AudioContext({ latencyHint: "interactive" });
  if (shared.state === "suspended") void shared.resume();
  return shared;
}

export class BrowserMic implements Mic {
  private stop: (() => void) | null = null;

  async open(heard: (pcm: Int16Array) => void): Promise<void> {
    const ctx = audioContext();
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    try {
      await ctx.audioWorklet.addModule(workletUrl);
    } catch (e) {
      stream.getTracks().forEach(t => t.stop());
      throw e;
    }
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "tars-capture", { numberOfInputs: 1, numberOfOutputs: 0 });
    node.port.onmessage = e => heard(new Int16Array(e.data as ArrayBuffer));
    source.connect(node);
    this.close();
    this.stop = () => {
      node.port.onmessage = null;
      source.disconnect();
      stream.getTracks().forEach(t => t.stop());
    };
  }

  close(): void {
    this.stop?.();
    this.stop = null;
  }
}

export class BrowserSpeaker implements Speaker {
  private rate = 24_000;
  private next = 0; // when the queued audio runs out, on the context's clock
  private playing = new Set<AudioBufferSourceNode>();
  private done: (() => void) | null = null;

  start(rate: number): void {
    this.cut();
    this.rate = rate;
  }

  audio(pcm: Int16Array): void {
    if (!pcm.length) return;
    const ctx = audioContext();
    const buffer = ctx.createBuffer(1, pcm.length, this.rate);
    buffer.copyToChannel(toFloat(pcm) as Float32Array<ArrayBuffer>, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const at = Math.max(this.next, ctx.currentTime + LEAD_S);
    source.start(at);
    this.next = at + buffer.duration;
    this.playing.add(source);
    source.onended = () => {
      this.playing.delete(source);
      this.check();
    };
  }

  end(played: () => void): void {
    this.done = played;
    this.check();
  }

  cut(): void {
    this.done = null;
    for (const source of this.playing) {
      source.onended = null;
      source.stop();
    }
    this.playing.clear();
    this.next = 0;
  }

  private check(): void {
    if (this.done && this.playing.size === 0) {
      const done = this.done;
      this.done = null;
      done();
    }
  }
}

interface Ticket {
  ticket: string;
  scheme: "ws" | "wss";
  port: number;
  person: string | null;
}

/** Why the page can't talk, said plainly: no HTTPS, no microphone at all. Null if it can try. */
export function cantTalk(): string | null {
  if (!window.isSecureContext) return "insecure";
  if (!navigator.mediaDevices?.getUserMedia || typeof AudioWorkletNode === "undefined") return "unsupported";
  return null;
}

/** Opens the body link with a fresh ticket, and keeps the page's half of it: the returned body, and a way to close. */
export async function connect(events: Events): Promise<{ body: PageBody; close: () => void; person: string | null }> {
  const got = await post<Ticket>("/api/talk/ticket");
  const ws = new WebSocket(`${got.scheme}://${location.hostname}:${got.port}`);
  ws.binaryType = "arraybuffer";
  const body = new PageBody(
    message => ws.readyState === WebSocket.OPEN && ws.send(message),
    new BrowserMic(),
    new BrowserSpeaker(),
    events,
  );
  let answered = false;
  ws.onmessage = e => {
    if (!answered && typeof e.data === "string" && e.data.includes('"challenge"')) {
      answered = true;
      ws.send(body.hello(got.ticket));
      return;
    }
    body.brain(e.data as string | ArrayBuffer);
  };
  ws.onclose = () => body.lost();
  return { body, close: () => ws.close(), person: got.person };
}
