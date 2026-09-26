"""Real people saying lookalike words, cut from LibriSpeech train-clean-100 with a forced aligner (MMS).

    DATA/eval/.venv/bin/python -m training.data.real_lookalikes

For every sentence containing a lookalike word ("stars", "cars", "guards", "tars"...), align the transcript
to the audio and cut the previous word plus the lookalike ("the stars", "his car"), with a little margin.
Output: DATA/real_lookalikes/<utterance>_<word>.wav
"""

import random

from training.common import Layout, parser

WORDS = {
    "STARS",
    "CARS",
    "BARS",
    "MARS",
    "GUITARS",
    "PARTS",
    "DARTS",
    "GUARDS",
    "TAR",
    "STAR",
    "CAR",
    "BAR",
    "TARS",
    "HEARTS",
    "CARDS",
    "TARTS",
    "CHARLES",
    "MARKS",
    "STARTS",
    "STOP",
    "HAY",
}
COMMON = {"THERE"}  # very frequent: take a 15% sample
MARGIN_S = 0.12


def main():
    p = parser(__doc__)
    p.add_argument("--threads", type=int, default=2)
    args = p.parse_args()
    import soundfile as sf
    import torch
    import torchaudio

    layout = Layout(args.data)
    out = layout.real_lookalikes
    torch.set_num_threads(args.threads)
    out.mkdir(parents=True, exist_ok=True)
    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model(with_star=False).eval()
    tokenizer, aligner = bundle.get_tokenizer(), bundle.get_aligner()
    rng = random.Random(0)
    lines = []
    for trans in sorted(layout.librispeech.glob("*/*/*.trans.txt")):
        for line in trans.read_text().splitlines():
            utt, *words = line.split()
            hits = [i for i, w in enumerate(words) if w in WORDS or (w in COMMON and rng.random() < 0.15)]
            if hits:
                lines.append((trans.parent / f"{utt}.flac", words, hits))
    print(f"{len(lines)} sentences with lookalike words", flush=True)
    kept = 0
    for n, (flac, words, hits) in enumerate(lines):
        audio, sr = sf.read(flac, dtype="float32")
        wav = torch.from_numpy(audio)[None]
        clean_words = ["".join(c for c in w.lower() if c.isalpha() or c == "'").replace("'", "") for w in words]
        with torch.inference_mode():
            emission, _ = model(wav)
            spans = aligner(emission[0], tokenizer(clean_words))
        frame_s = wav.shape[1] / emission.shape[1] / sr
        for i in hits:
            start_word = max(0, i - 1)
            t0 = max(0.0, spans[start_word][0].start * frame_s - MARGIN_S)
            t1 = min(len(audio) / sr, spans[i][-1].end * frame_s + MARGIN_S)
            clip = audio[int(t0 * sr) : int(t1 * sr)]
            if 0.3 < len(clip) / sr < 2.5:
                sf.write(out / f"{flac.stem}_{words[i].lower()}.wav", clip, sr, subtype="PCM_16")
                kept += 1
        if n % 200 == 0:
            print(f"{n}/{len(lines)} sentences, {kept} clips", flush=True)
    print(f"done: {kept} clips", flush=True)


if __name__ == "__main__":
    main()
