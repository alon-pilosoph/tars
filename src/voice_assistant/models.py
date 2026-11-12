"""The small local models TARS downloads on first use, into models/ (gitignored)."""

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

# The longest a download may go without a byte, so a stalled one fails the first start instead of hanging it.
DOWNLOAD_TIMEOUT_S = 30.0


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise urllib.error.URLError(f"a model download was redirected off HTTPS, to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(HttpsOnlyRedirects)


def fetch(path: Path, url: str, what: str, sha256: str) -> Path:
    """Downloads `url` to `path` unless it's there, and keeps it only if its SHA-256 is `sha256`."""
    if not url.startswith("https://"):
        raise ValueError(f"{what} is only downloaded over HTTPS, not from {url}")
    if not path.exists():
        print(f"Downloading {what} to {path}...")
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        digest = hashlib.sha256()
        try:
            with _opener.open(url, timeout=DOWNLOAD_TIMEOUT_S) as response, partial.open("wb") as out:
                while block := response.read(1 << 20):
                    digest.update(block)
                    out.write(block)
            if digest.hexdigest() != sha256:
                raise ValueError(f"{what} from {url} isn't the expected file (SHA-256 {digest.hexdigest()})")
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(path)
    return path


def onnx_session(path: Path, url: str, what: str, sha256: str):
    """Single-threaded: these models run on every audio block, alongside everything else, on a Pi."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    return ort.InferenceSession(
        str(fetch(path, url, what, sha256)), sess_options=options, providers=["CPUExecutionProvider"]
    )
