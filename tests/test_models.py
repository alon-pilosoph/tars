"""The small models downloaded on first use: written whole, or not at all, and never waited on forever."""

import socket
import threading

from voice_assistant import models


def test_a_model_is_downloaded_once(tmp_path):
    source = tmp_path / "served.onnx"
    source.write_bytes(b"weights")
    path = tmp_path / "models" / "model.onnx"
    assert models.fetch(path, source.as_uri(), "a model").read_bytes() == b"weights"
    source.unlink()
    assert models.fetch(path, source.as_uri(), "a model") == path  # there already: not asked for again


def test_a_stalled_download_fails_instead_of_hanging_the_first_start(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DOWNLOAD_TIMEOUT_S", 0.2)
    server = socket.create_server(("127.0.0.1", 0))
    accepted = []
    threading.Thread(target=lambda: accepted.append(server.accept()), daemon=True).start()  # and never answers
    path = tmp_path / "model.onnx"
    failed = []

    def download():
        try:
            models.fetch(path, f"http://127.0.0.1:{server.getsockname()[1]}/model.onnx", "a model")
        except OSError as e:
            failed.append(e)

    downloading = threading.Thread(target=download, daemon=True)
    downloading.start()
    downloading.join(5)
    server.close()
    assert failed and not path.exists() and not path.with_suffix(".onnx.part").exists()
