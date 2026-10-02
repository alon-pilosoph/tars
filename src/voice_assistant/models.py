"""The small local models TARS downloads on first use, into models/ (gitignored)."""

import urllib.request
from pathlib import Path


def fetch(path: Path, url: str, what: str) -> Path:
    if not path.exists():
        print(f"Downloading {what} to {path}...")
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, partial)
        partial.replace(path)
    return path


def onnx_session(path: Path, url: str, what: str):
    """Single-threaded: these models run on every audio block, alongside everything else, on a Pi."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    return ort.InferenceSession(str(fetch(path, url, what)), sess_options=options, providers=["CPUExecutionProvider"])
