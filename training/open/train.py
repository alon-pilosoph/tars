"""The open stage 1: small classifiers on openWakeWord's pretrained speech embedding (Google's, trained on a lot of
audio), trained only on generic data, never anyone's real recordings. Four heads are averaged into one openWakeWord
model (models/generic/hey_tars_open.onnx). Runs in DATA/oww/.venv (openWakeWord and PyTorch), from the repo root:

    DATA/oww/.venv/bin/python -m training.open.train embed           # training rows from the clips
    DATA/oww/.venv/bin/python -m training.open.train train --out NAME [--head convattn] [--near 3] [--neg 10]
    DATA/oww/.venv/bin/python -m training.open.train mine --model NAME --out NAME ...   # hard negatives
    DATA/oww/.venv/bin/python -m training.open.train export --heads A,B,C,D --out FILE.onnx

recipe.sh runs them in order. Each example is 2 s ending 0.3 s after the phrase, so a head sees the embedding's last
16 frames (1.28 s). Positives: the synthetic "hey TARS" clips (Piper LibriTTS and more voices, Kokoro, OpenAI,
prosody) with training TV or babble at clean/15/10/5/0/-5 dB, sometimes in a simulated room. Negatives: the synthetic
lookalikes the same way, real read speech and conversation, stage 1's mined mistakes, TV or babble alone, and the
heads' own mistakes on more training audio. `embed --loud` adds more positives and lookalikes at +5 to -10 dB only.
"""

import argparse
import os
import random
from pathlib import Path

import numpy as np
import soundfile as sf

D = Path(os.environ.get("TARS_TRAINING_DATA", Path.home() / "tars-training"))
OUT = D / "open_model"
SR, CLIP = 16000, 32000
SNRS = [None, 15, 10, 5, 0, -5]
LOUD_SNRS = [5, 0, -5, -10]
TEMPERATURE = 8.0  # on the logit at export: the same ranking, but scores that don't pile up at exactly 1.0


def read(p: Path) -> np.ndarray | None:
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    return x.mean(axis=1) if sr == SR else None


def fit_end(x: np.ndarray, tail: float = 0.3) -> np.ndarray:
    """2 s ending `tail` s after `x` ends (zeros before, or its last 2 s)."""
    y = np.concatenate([x, np.zeros(int(tail * SR), np.float32)])
    return np.concatenate([np.zeros(max(0, CLIP - len(y)), np.float32), y])[-CLIP:]


class Noise:
    def __init__(self, rng: np.random.Generator, snrs: list):
        self.rng, self.snrs = rng, snrs
        self.tv = sorted((D / "aug/train/tv").glob("*.wav"))
        self.babble = sorted((D / "aug/train/babble").glob("*.wav"))
        self.rirs = sorted((D / "aug/train_rirs").glob("*.wav"))
        self._cache: dict = {}

    def load(self, p: Path) -> np.ndarray:
        if p not in self._cache:
            self._cache[p] = read(p)
        return self._cache[p]

    def bed(self, n: int) -> np.ndarray:
        a = self.load(self.rng.choice(self.tv if self.rng.random() < 0.6 else self.babble))
        s = self.rng.integers(0, max(1, len(a) - n))
        b = a[s : s + n]
        return np.pad(b, (0, n - len(b)))

    def apply(self, x: np.ndarray) -> np.ndarray:
        if self.rirs and self.rng.random() < 0.3:
            from scipy.signal import fftconvolve

            r = self.load(self.rng.choice(self.rirs))
            x = fftconvolve(x, r / (np.abs(r).max() + 1e-9))[: len(x)].astype(np.float32)
        snr = self.snrs[self.rng.integers(len(self.snrs))]
        if snr is not None:
            b = self.bed(len(x))
            rx, rb = np.sqrt(np.mean(x**2)) + 1e-6, np.sqrt(np.mean(b**2)) + 1e-6
            x = x + b * (rx / rb) / 10 ** (snr / 20)
        return x


def embed_rows(clips: list, features, batch: int = 256) -> np.ndarray:
    rows = []
    for i in range(0, len(clips), batch):
        chunk = np.stack(clips[i : i + batch])
        pcm = (np.clip(chunk / max(1.0, np.abs(chunk).max() / 0.95), -1, 1) * 32767).astype(np.int16)
        rows.append(features.embed_clips(pcm, batch_size=batch)[:, -16:, :])
    return np.concatenate(rows).astype(np.float32)


def embed(loud: bool) -> None:
    from openwakeword.utils import AudioFeatures

    noise = Noise(np.random.default_rng(7 if loud else 3), LOUD_SNRS if loud else SNRS)
    features = AudioFeatures(inference_framework="onnx")
    OUT.mkdir(parents=True, exist_ok=True)

    def clips(sub: str, n: int, seed: int) -> list:
        fs = []
        for src in ("piper_libritts", "piper_voices", "kokoro", "openai", "prosody"):
            fs += sorted((D / "clips" / src / "hey_tars" / sub).glob("*.wav"))
        random.Random(seed).shuffle(fs)
        return fs[:n]

    def speech(folder: str, n: int, seed: int) -> list:
        fs = sorted((D / folder).rglob("*.wav"))
        random.Random(seed).shuffle(fs)
        return fs[:n]

    if loud:
        sets = [("posloud", clips("positive", 12000, 11), 1, True), ("nearloud", clips("near_miss", 9000, 12), 0, True)]
    else:
        sets = [
            ("pos", clips("positive", 16000, 1), 1, True),
            ("near", clips("near_miss", 14000, 2), 0, True),
            ("speech", speech("aug/speech_negatives", 7000, 3), 0, True),
            ("conv", speech("aug/conv_negatives", 7000, 4), 0, True),
            ("mined", speech("aug/mined", 9905, 5), 0, False),
        ]
    for name, files, label, augment in sets:
        path = OUT / f"rows-{name}.npz"
        if path.exists():
            continue
        made = []
        for f in files:
            x = read(f)
            if x is None or not len(x):
                continue
            x = fit_end(x) if label else np.pad(x, (0, max(0, CLIP - len(x))))[-CLIP:]
            made.append(noise.apply(x) if augment else x)
        X = embed_rows(made, features)
        np.savez(path, X=X, y=np.full(len(X), label, np.int8))
        print(name, X.shape, flush=True)
    path = OUT / "rows-bed.npz"
    if not loud and not path.exists():
        X = embed_rows([noise.bed(CLIP) for _ in range(6000)], features)
        np.savez(path, X=X, y=np.zeros(len(X), np.int8))
        print("bed", X.shape, flush=True)


def model(torch, head: str):
    nn = torch.nn
    if head == "dnn":
        return nn.Sequential(
            nn.Flatten(),
            nn.Linear(16 * 96, 128),
            nn.LayerNorm(128),
            nn.ReLU(),
            nn.Linear(128, 128),
            nn.LayerNorm(128),
            nn.ReLU(),
            nn.Linear(128, 1),
        )

    class ConvAttn(nn.Module):
        """LiveKit's wake-word head: Conv1D blocks, self-attention, mean pool."""

        def __init__(self):
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv1d(96, 64, 3, padding=1),
                nn.BatchNorm1d(64),
                nn.ReLU(),
                nn.Conv1d(64, 64, 3, padding=1),
                nn.BatchNorm1d(64),
                nn.ReLU(),
            )
            self.attn = nn.MultiheadAttention(64, 4, batch_first=True)
            self.out = nn.Linear(64, 1)

        def forward(self, x):  # batch x 16 x 96
            h = self.conv(x.transpose(1, 2)).transpose(1, 2)
            a, _ = self.attn(h, h, h)
            return self.out((h + a).mean(dim=1))

    return ConvAttn()


def train(out: str, head: str, near: float, neg: float) -> None:
    """A head on every rows-*.npz there is. Negatives count `neg` times more than positives (a false answer costs more
    than a miss), lookalikes `near` times more than other negatives."""
    import torch

    paths = sorted(OUT.glob("rows-*.npz"))
    rows = [np.load(p) for p in paths]
    X = torch.from_numpy(np.concatenate([r["X"] for r in rows]))
    y = torch.from_numpy(np.concatenate([r["y"] for r in rows]).astype(np.float32))
    names = np.concatenate([[p.stem[5:]] * len(r["y"]) for p, r in zip(paths, rows, strict=True)])
    g = torch.Generator().manual_seed(0)
    perm = torch.randperm(len(y), generator=g)
    val, tr = perm[: len(y) // 10], perm[len(y) // 10 :]
    m = model(torch, head)
    opt = torch.optim.AdamW(m.parameters(), lr=1e-3, weight_decay=1e-3)
    pos_w = (y[tr] == 0).sum() / (y[tr] == 1).sum()
    weights = torch.from_numpy((np.isin(names, ["near", "nearloud"]) * (near - 1) + 1.0).astype(np.float32))
    bce = torch.nn.BCEWithLogitsLoss(pos_weight=pos_w / neg, reduction="none")
    for epoch in range(30):
        m.train()
        for idx in torch.randperm(len(tr), generator=g).split(512):
            b = tr[idx]
            opt.zero_grad()
            loss = (bce(m(X[b]).squeeze(1), y[b]) * weights[b]).mean()
            loss.backward()
            opt.step()
        if epoch % 5 == 4:
            m.eval()
            with torch.no_grad():
                p = torch.sigmoid(m(X[val]).squeeze(1)).numpy()
            nv = names[val.numpy()]
            parts = "  ".join(f"{n} {100 * np.mean(p[nv == n] >= 0.5):.1f}%" for n in sorted(set(nv)))
            print(f"epoch {epoch + 1}: over 0.5 on held-out rows: {parts}", flush=True)
    torch.save({"head": head, "state": m.state_dict()}, OUT / f"{out}.pt")


def load(torch, name: str):
    saved = torch.load(OUT / f"{name}.pt")
    m = model(torch, saved["head"])
    m.load_state_dict(saved["state"])
    return m.eval()


def mine(name: str, out: str, hours: float, keep_over: float, offset: int) -> None:
    """Hard negatives: head `name` run over training-only audio (the training TV and babble, LibriSpeech
    train-clean-100, People's Speech from `offset` on), every stretch it scores over `keep_over` kept as a row."""
    import torch
    from openwakeword.utils import AudioFeatures

    m = load(torch, name)
    features = AudioFeatures(inference_framework="onnx")
    rng = random.Random(9)
    files = sorted((D / "aug/train/tv").glob("*.wav")) + sorted((D / "aug/train/babble").glob("*.wav"))
    libri = sorted((D / "LibriSpeech/train-clean-100").rglob("*.flac"))
    conv = sorted((D / "peoples_speech/wav").glob("*.wav"))
    rng.shuffle(libri)
    rng.shuffle(conv)
    files += libri[offset : offset + 3000] + conv[offset : offset + 3000]
    rows, seconds = [], 0.0
    for f in files:
        if seconds > hours * 3600:
            break
        x = read(f)
        if x is None or len(x) < CLIP:
            continue
        seconds += len(x) / SR
        pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
        pcm = pcm[: len(pcm) // 1280 * 1280]  # whole 80 ms frames: other lengths trip openWakeWord's batch features
        e = features.embed_clips(pcm[None], batch_size=1)[0]
        if len(e) < 16:
            continue
        stacks = np.stack([e[i : i + 16] for i in range(0, len(e) - 15, 2)]).astype(np.float32)
        with torch.no_grad():
            scores = torch.sigmoid(m(torch.from_numpy(stacks))).squeeze(1).numpy()
        rows += list(stacks[scores >= keep_over])
    X = np.stack(rows) if rows else np.zeros((0, 16, 96), np.float32)
    np.savez(OUT / f"rows-{out}.npz", X=X, y=np.zeros(len(X), np.int8))
    print(f"mined {len(X)} hard negatives from {seconds / 3600:.1f} h", flush=True)


def export(heads: list[str], path: Path) -> None:
    """The heads as one openWakeWord model: ONNX, input 1 x 16 x 96 (the embedding's last 16 frames), one score, the
    average of each head's tempered score."""
    import torch

    class Averaged(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.heads = torch.nn.ModuleList([load(torch, h) for h in heads])

        def forward(self, x):
            return torch.stack([torch.sigmoid(h(x) / TEMPERATURE) for h in self.heads]).mean(dim=0)

    torch.onnx.export(
        Averaged(),
        torch.zeros(1, 16, 96),
        str(path),
        input_names=["x"],
        output_names=["score"],
        opset_version=17,
        dynamo=False,
    )
    print("exported", path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("embed")
    e.add_argument("--loud", action="store_true")
    t = sub.add_parser("train")
    t.add_argument("--out", required=True)
    t.add_argument("--head", choices=["dnn", "convattn"], default="dnn")
    t.add_argument("--near", type=float, default=1.0)
    t.add_argument("--neg", type=float, default=3.0)
    m = sub.add_parser("mine")
    m.add_argument("--model", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--hours", type=float, default=12.0)
    m.add_argument("--keep-over", type=float, default=0.3)
    m.add_argument("--offset", type=int, default=0)
    x = sub.add_parser("export")
    x.add_argument("--heads", required=True, help="comma-separated head names")
    x.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    if a.cmd == "embed":
        embed(a.loud)
    elif a.cmd == "train":
        train(a.out, a.head, a.near, a.neg)
    elif a.cmd == "mine":
        mine(a.model, a.out, a.hours, a.keep_over, a.offset)
    else:
        export(a.heads.split(","), a.out)


if __name__ == "__main__":
    main()
