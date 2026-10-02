"""VCTK's 110 real speakers (many English accents), ~160 s of each at 16 kHz, from a Hugging Face parquet mirror.

    DATA/eval/.venv/bin/python -m training.data.prep_vctk

One shard at a time: download, keep up to MATCH_SECONDS per speaker, delete the shard.
Output: DATA/vctk/<speaker>/<text_id>.wav plus speakers.tsv (speaker, gender, accent, region).
"""

import io
from pathlib import Path

import numpy as np

from training.common import SR, Layout, log, parser

REPO_ID = "sanchit-gandhi/vctk"
MATCH_SECONDS = 160


def main():
    args = parser(__doc__).parse_args()
    import pyarrow.parquet as pq
    import soundfile as sf
    from huggingface_hub import HfApi, hf_hub_download
    from scipy.signal import resample_poly

    out = Layout(args.data).vctk
    tmp = out.parent / "vctk_shards"
    out.mkdir(parents=True, exist_ok=True)
    have = {p.name: sum(sf.info(f).duration for f in p.glob("*.wav")) for p in out.iterdir() if p.is_dir()}
    meta = {}
    shards = sorted(f for f in HfApi().list_repo_files(REPO_ID, repo_type="dataset") if f.endswith(".parquet"))
    for n, shard in enumerate(shards):
        path = hf_hub_download(REPO_ID, shard, repo_type="dataset", local_dir=tmp)
        for batch in pq.ParquetFile(path).iter_batches(
            batch_size=64, columns=["speaker_id", "audio", "text_id", "gender", "accent", "region"]
        ):
            for row in batch.to_pylist():
                spk = row["speaker_id"]
                meta[spk] = (row["gender"], row["accent"], row["region"])
                if have.get(spk, 0.0) >= MATCH_SECONDS:
                    continue
                audio, sr = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                if sr != SR:
                    audio = resample_poly(audio, SR, sr).astype(np.float32)
                (out / spk).mkdir(exist_ok=True)
                sf.write(out / spk / f"{row['text_id']}.wav", audio, SR, subtype="PCM_16")
                have[spk] = have.get(spk, 0.0) + len(audio) / SR
        Path(path).unlink()
        log(f"shard {n + 1}/{len(shards)} done; {len(have)} speakers so far")
    (out / "speakers.tsv").write_text("".join(f"{s}\t{g}\t{a}\t{r}\n" for s, (g, a, r) in sorted(meta.items())))
    log("DONE")


if __name__ == "__main__":
    main()
