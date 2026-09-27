"""Background audio for the wake model's augmentation: MIT room echoes, AudioSet noise, FMA music, all at 16 kHz.

    DATA/mww/.venv/bin/python -m training.data.backgrounds

The same sources as the first openWakeWord runs. Output: DATA/backgrounds/{mit_rirs,audioset_16k,fma}. Resumable.
"""

import io
import subprocess
import urllib.request
import zipfile
from pathlib import Path

import librosa
import numpy as np
import scipy.io.wavfile
import soundfile as sf

from training.common import Layout, parser

FMA_SMALL = "https://os.unil.cloud.switch.ch/fma/fma_small.zip"
FMA_TRACKS = 120


def n_wavs(folder):
    return len(list(folder.glob("*.wav")))


def write16k(path, y, sr):
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
    if n_wavs(fma) < FMA_TRACKS:
        fma.mkdir(exist_ok=True)
        # The first tracks (in the zip's order) of the official fma_small.zip (7.2 GB), read over HTTP ranges: ~60 MB
        # instead of all of it. The buffer turns zipfile's many small reads into a few large requests.
        with zipfile.ZipFile(io.BufferedReader(RangeFile(FMA_SMALL), buffer_size=1 << 20)) as z:
            for name in sorted(n for n in z.namelist() if n.endswith(".mp3"))[:FMA_TRACKS]:
                target = fma / Path(name).with_suffix(".wav").name
                if not target.exists():
                    y, sr = librosa.load(io.BytesIO(z.read(name)), sr=None, mono=True)
                    write16k(target, y, sr)
    print("fma", n_wavs(fma))


class RangeFile(io.RawIOBase):
    """A remote file that zipfile can seek in: every read is an HTTP range request."""

    def __init__(self, url: str):
        self._url, self._pos = url, 0
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD")) as r:
            self._size = int(r.headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        self._pos = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence] + offset
        return self._pos

    def readinto(self, buffer) -> int:
        end = min(self._size, self._pos + len(buffer))
        if end <= self._pos:
            return 0
        request = urllib.request.Request(self._url, headers={"Range": f"bytes={self._pos}-{end - 1}"})
        with urllib.request.urlopen(request) as r:
            data = r.read()
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)


if __name__ == "__main__":
    main()
