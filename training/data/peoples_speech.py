"""Real conversational speech (MLCommons People's Speech, its CC-BY "clean" part): shards downloaded, each utterance
written as a 16 kHz WAV, to be cut into negatives (stage1/features.py cuts conv) and as stage 2's "no wake phrase"
conversation (stage2/train_check.py --hard).

    uv run --group training --with pyarrow python -m training.data.peoples_speech [--shards 20]

Output: DATA/peoples_speech/wav (20 shards: about 145 hours). The shards aren't kept. Resumable.
"""

import io
import os
import random

import numpy as np
import soundfile as sf

from training.common import SR, Layout, log, parser

DATASET = "MLCommons/peoples_speech"


def main():
    p = parser(__doc__)
    p.add_argument("--shards", type=int, default=20)
    args = p.parse_args()
    import pyarrow.parquet as pq
    from huggingface_hub import HfApi, hf_hub_download

    root = Layout(args.data).root / "peoples_speech"
    wav = root / "wav"
    wav.mkdir(parents=True, exist_ok=True)
    siblings = HfApi().dataset_info(DATASET).siblings
    files = sorted(s.rfilename for s in siblings if s.rfilename.startswith("clean/train-"))
    random.Random(19).shuffle(files)
    hours = 0.0
    for name in files[: args.shards]:
        done = root / (name.rsplit("/", 1)[-1].removesuffix(".parquet") + ".done")
        if done.exists():
            continue
        path = hf_hub_download(DATASET, name, repo_type="dataset", local_dir=str(root))
        for row in pq.read_table(path, columns=["id", "audio"]).to_pylist():
            x, sr = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
            x = x.mean(axis=1) if x.ndim > 1 else x
            if sr != SR:
                from scipy.signal import resample_poly

                g = np.gcd(sr, SR)
                x = resample_poly(x, SR // g, sr // g)
            out = wav / f"{row['id'].replace('/', '_')}.wav"
            sf.write(str(out), (np.clip(x, -1, 1) * 32767).astype(np.int16), SR, subtype="PCM_16")
            hours += len(x) / SR / 3600
        os.remove(path)
        done.touch()
        log(f"{name}: {hours:.1f} h so far")
    log(f"done: {len(list(wav.glob('*.wav')))} utterances")


if __name__ == "__main__":
    main()
