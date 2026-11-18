/* The microphone's audio, as the body link wants it: 16 kHz mono 16-bit (docs/bodies.md). A browser records at its
   own rate (48 or 44.1 kHz, sometimes others), so each 16 kHz sample is the average of the samples it spans, which
   keeps out most of what's above 8 kHz on the way down. Chunks of any length give what one long chunk would. */

export const LINK_RATE = 16_000;

/** A downsampler from `from` Hz, fed float chunks (-1..1), giving 16-bit samples at `to` Hz. */
export function downsampler(from: number, to = LINK_RATE): (input: Float32Array) => Int16Array {
  const ratio = from / to;
  let seen = 0; // samples taken in so far
  let edge = ratio; // where the output sample being gathered ends, in input samples
  let sum = 0;
  let count = 0;
  return input => {
    const out = new Int16Array(Math.ceil((input.length + 1) / ratio) + 1);
    let n = 0;
    for (let i = 0; i < input.length; i++, seen++) {
      if (seen >= edge) {
        out[n++] = toInt16(sum / count);
        sum = count = 0;
        edge += ratio;
      }
      sum += input[i];
      count++;
    }
    return out.slice(0, n);
  };
}

export function toInt16(x: number): number {
  const clipped = Math.max(-1, Math.min(1, x));
  return Math.round(clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff);
}

/** 16-bit samples as floats, for playing the brain's audio. */
export function toFloat(pcm: Int16Array): Float32Array {
  const out = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 0x8000;
  return out;
}
