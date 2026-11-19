"""The extraction server for [extract] (src/voice_assistant/extract.py): POST /extract with an .npz of `mix` and
`enroll` (16 kHz int16), answered with an .npz of `voice` (float32), the enrolled person's voice taken out of the mix.
WeSep's pretrained target speaker extraction (BSRNN + ECAPA, VoxCeleb, Apache-2.0: the wesep_2024 branch of
github.com/wenet-e2e/wesep), on a GPU if there is one: about 0.2 s for a 3 s window on an Apple M-series GPU, 1.9 s
on its CPU, 23 s on a Pi 4.

Not part of TARS's environment (PyTorch alone is gigabytes); set it up apart:

    uv venv -p 3.12 ~/tse && git clone -b wesep_2024 https://github.com/wenet-e2e/wesep ~/tse/wesep
    VIRTUAL_ENV=~/tse uv pip install -e ~/tse/wesep pyyaml librosa kaldiio requests tqdm torchaudio silero-vad \
        "wespeaker @ git+https://github.com/wenet-e2e/wespeaker.git@58071902625ae6b072befda2932a38e9b38d5f26"
    ~/tse/bin/python tools/extract_server.py --host 0.0.0.0 [--port 8765]   # reachable from the Pi

On Apple GPUs, wesep's models/bsrnn.py needs torch.hann_window(..., device=, dtype=) in place of .type(): its
.type(wav_input.type()) is refused there.
"""

import argparse
import io
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

import numpy as np

MAX_BODY = 8 * 1024 * 1024


def pack(**arrays) -> bytes:
    buffer = io.BytesIO()
    np.savez(buffer, **arrays)
    return buffer.getvalue()


def main():
    import torch
    import wesep

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve other machines (the Pi)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cpu")
    a = p.parse_args()
    extractor = wesep.load_model("english")
    extractor.set_device(a.device)
    extractor.set_output_norm(False)
    lock = Lock()  # one extraction at a time: the model isn't shared safely

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", 0))
            if self.path != "/extract" or not 0 < size <= MAX_BODY:
                self.send_error(400)
                return
            try:
                with np.load(io.BytesIO(self.rfile.read(size)), allow_pickle=False) as z:
                    mix = torch.from_numpy(z["mix"].astype(np.float32) / 32768)[None]
                    enroll = torch.from_numpy(z["enroll"].astype(np.float32) / 32768)[None]
                t = time.time()
                with lock:
                    voice = extractor.extract_speech_from_pcm(mix, 16000, enroll, 16000)
                body = pack(voice=voice[0].numpy().astype(np.float32))
                print(f"extracted {mix.shape[1] / 16000:.1f} s in {time.time() - t:.2f} s", flush=True)
            except Exception as e:  # noqa: BLE001 - a bad request gets an error, the server carries on
                self.send_error(500, repr(e)[:200])
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"extraction server on {a.host}:{a.port} ({a.device})", file=sys.stderr, flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
