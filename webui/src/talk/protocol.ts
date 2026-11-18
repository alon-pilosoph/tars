/* The Talk page's half of the body link (docs/bodies.md), held to the same rules as the Python body
   (bodyclient.BodyProtocol) and the Atom: nothing goes up before a tap, `listen on` is taken only inside a
   conversation the page opened, and a conversation streams for at most its cap. No browser in here: the microphone,
   the speaker and the socket are handed in, so tests drive it with fakes. */

export const PROTOCOL = 1;
const PAGE_CEILING_S = 120; // the most a conversation streams, whatever the brain says
const LINK_RATE = 16_000;

export type TalkState = "connecting" | "ready" | "listening" | "thinking" | "speaking" | "gone";
export interface Line {
  who: "you" | "tars";
  text: string;
}

export interface Mic {
  /** Starts sending 16 kHz 16-bit pieces to `heard`. Rejects if the browser refuses the microphone. */
  open(heard: (pcm: Int16Array) => void): Promise<void>;
  close(): void;
}

export interface Speaker {
  start(rate: number): void;
  audio(pcm: Int16Array): void;
  /** All the audio is in: `played` once the last of it has left the speaker. */
  end(played: () => void): void;
  cut(): void;
}

export interface Events {
  state(state: TalkState): void;
  line(line: Line): void;
  /** The microphone couldn't be opened: why, as the browser said. */
  refused(why: string): void;
}

export class PageBody {
  state: TalkState = "connecting";
  private welcomed = false;
  private awake = false; // in a conversation this page opened, until the brain sends it to sleep (or the cap)
  private listening = false; // streaming: awake, and the brain hasn't said `listen off`
  private playing: number | null = null;
  private playRate = 24_000;
  private maxListenS = PAGE_CEILING_S;
  private streamed = 0; // samples up in this conversation

  constructor(
    private send: (message: string | ArrayBuffer) => void,
    private mic: Mic,
    private speaker: Speaker,
    private on: Events,
  ) {}

  /** The page's hello, answering the brain's challenge: a ticket in place of a device key. */
  hello(ticket: string): string {
    return JSON.stringify({
      type: "hello",
      protocol: PROTOCOL,
      ticket,
      kind: "web",
      wake: "none",
      echo_cancel: true,
      buttons: ["talk"],
      version: "talk-page",
    });
  }

  // What the brain sends

  brain(message: string | ArrayBuffer): void {
    if (typeof message !== "string") {
      if (this.playing !== null && message.byteLength >= 2) {
        this.speaker.audio(new Int16Array(message, 0, message.byteLength >> 1));
      }
      return;
    }
    let said: Record<string, unknown>;
    try {
      said = JSON.parse(message);
    } catch {
      return;
    }
    switch (said.type) {
      case "welcome":
        this.welcomed = true;
        this.playRate = Number(said.play_rate) || this.playRate;
        this.maxListenS = Math.min(PAGE_CEILING_S, Number(said.max_listen_s) || PAGE_CEILING_S);
        break; // ready once the brain's first `sleep` says it's waiting: a tap before that would be dropped
      case "listen":
        // Only inside a conversation the page opened: never from sleep, whatever the brain says.
        this.listening = Boolean(said.on) && this.awake;
        if (this.awake && this.playing === null) this.become(this.listening ? "listening" : "thinking");
        break;
      case "play":
        if (this.playing !== null) this.speaker.cut();
        this.playing = Number(said.id);
        this.listening = this.listening && Boolean(said.listening);
        this.speaker.start(this.playRate);
        this.become("speaking");
        break;
      case "play_end": {
        const id = Number(said.id);
        if (id !== this.playing) break;
        this.speaker.end(() => this.played(id, false));
        break;
      }
      case "stop":
        this.cut();
        break;
      case "sleep":
        this.asleep();
        break;
      case "text":
        if ((said.who === "you" || said.who === "tars") && typeof said.text === "string") {
          this.on.line({ who: said.who, text: said.text });
        }
        break;
    }
  }

  // What happens here

  /** The big button: talk, or, while TARS speaks, stop it. */
  async tap(): Promise<void> {
    if (this.state === "speaking") {
      this.sendJson({ type: "button", name: "stop" });
      this.cut();
      return;
    }
    if (this.state !== "ready" || this.awake) return;
    this.awake = this.listening = true;
    this.streamed = 0;
    this.become("listening");
    // The wake first: audio that came before it would be dropped by the brain.
    this.sendJson({ type: "wake", how: "button", score: 1, preroll_ms: 0 });
    try {
      await this.mic.open(pcm => this.heard(pcm));
    } catch (e) {
      this.sendJson({ type: "gone_quiet" });
      this.asleep();
      this.become("ready");
      this.on.refused(e instanceof Error ? e.name || e.message : String(e));
    }
  }

  /** The connection dropped: whatever was going on is over. */
  lost(): void {
    this.welcomed = false;
    this.speaker.cut();
    this.playing = null;
    this.asleep();
    this.become("gone");
  }

  private heard(pcm: Int16Array): void {
    if (!this.listening) return;
    this.streamed += pcm.length;
    if (this.streamed > this.maxListenS * LINK_RATE) {
      this.sendJson({ type: "gone_quiet" });
      this.asleep();
      return;
    }
    const copy = pcm.slice();
    this.send(copy.buffer);
  }

  private cut(): void {
    if (this.playing === null) return;
    const id = this.playing;
    this.speaker.cut();
    this.played(id, true);
  }

  private played(id: number, cut: boolean): void {
    if (this.playing !== id) return;
    this.playing = null;
    this.sendJson({ type: "played", id, cut });
    if (this.awake) this.become(this.listening ? "listening" : "thinking");
    else if (this.welcomed) this.become("ready");
  }

  private asleep(): void {
    this.awake = this.listening = false;
    this.mic.close();
    if (this.playing === null && this.welcomed) this.become("ready");
  }

  private become(state: TalkState): void {
    if (state === this.state) return;
    this.state = state;
    this.on.state(state);
  }

  private sendJson(message: object): void {
    this.send(JSON.stringify(message));
  }
}
