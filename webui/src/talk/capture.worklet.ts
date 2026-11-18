/* The Talk page's microphone, inside the browser's audio thread: each block it records, downsampled to 16 kHz and
   posted on as 20 ms pieces of 16-bit audio. */
import { downsampler } from "./resample";

declare const sampleRate: number; // the AudioWorkletGlobalScope's: the recording's rate
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}
declare function registerProcessor(name: string, processor: unknown): void;

const PIECE = 320; // 20 ms at 16 kHz

class Capture extends AudioWorkletProcessor {
  private down = downsampler(sampleRate);
  private held = new Int16Array(PIECE);
  private filled = 0;

  process(inputs: Float32Array[][]): boolean {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const sample of this.down(channel)) {
      this.held[this.filled++] = sample;
      if (this.filled === PIECE) {
        const piece = this.held.slice();
        this.port.postMessage(piece.buffer, [piece.buffer]);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("tars-capture", Capture);
