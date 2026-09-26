"""Background audio for the wake model's augmentation: MIT room echoes, AudioSet noise, FMA music, all at 16 kHz.

    DATA/mww/.venv/bin/python -m training.data.backgrounds

The same sources as the first openWakeWord runs. Output: DATA/backgrounds/{mit_rirs,audioset_16k,fma}. Resumable.
"""

import io
import subprocess

import numpy as np
import scipy.io.wavfile
import soundfile as sf

from training.common import Layout, parser


def n_wavs(folder):
    return len(list(folder.glob("*.wav")))


def write16k(path, y, sr):
    import librosa

    y = y.mean(axis=1) if y.ndim > 1 else y
    if sr != 16000:
        y = librosa.resample(y, orig_sr=sr, target_sr=16000)
    scipy.io.wavfile.write(path, 16000, (np.clip(y, -1, 1) * 32767).astype(np.int16))


def main():
    args = parser(__doc__).parse_args()
    out = Layout(args.data).backgrounds
    out.mkdir(parents=True, exist_ok=True)

    rirs = out / "mit_rirs"
    if n_wavs(rirs) < 250:
        src = out / "MIT_environmental_impulse_responses"
        if not src.exists():
            url = "https://huggingface.co/datasets/davidscripka/MIT_environmental_impulse_responses"
            subprocess.run(["git", "clone", "-q", url, str(src)], check=True)
        rirs.mkdir(exist_ok=True)
        for p in sorted((src / "16khz").glob("*.wav")):
            y, sr = sf.read(p, dtype="float32")
            write16k(rirs / p.name, y, sr)
    print("mit_rirs", n_wavs(rirs))

    audioset = out / "audioset_16k"
    if n_wavs(audioset) < 450:
        import pyarrow.parquet as pq

        parquet = out / "as09.parquet"
        url = "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/09.parquet"
        subprocess.run(["curl", "-sL", "-C", "-", "-o", str(parquet), url], check=True)
        audioset.mkdir(exist_ok=True)
        table = pq.read_table(parquet, columns=["video_id", "audio"])
        for vid, a in zip(table.column("video_id").to_pylist(), table.column("audio").to_pylist()):
            y, sr = sf.read(io.BytesIO(a["bytes"]), dtype="float32")
            write16k(audioset / f"{vid}.wav", y, sr)
    print("audioset_16k", n_wavs(audioset))

    fma = out / "fma"
    if n_wavs(fma) < 100:
        import datasets

        fma.mkdir(exist_ok=True)
        rows = iter(
            datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True).cast_column(
                "audio", datasets.Audio(sampling_rate=16000)
            )
        )
        for _ in range(120):
            row = next(rows)
            name = row["audio"]["path"].split("/")[-1].replace(".mp3", ".wav")
            scipy.io.wavfile.write(fma / name, 16000, (row["audio"]["array"] * 32767).astype(np.int16))
    print("fma", n_wavs(fma))


if __name__ == "__main__":
    main()
