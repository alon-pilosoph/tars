"""The small models downloaded on first use: written whole, or not at all, and never waited on forever."""

import io
import socket
import threading
import urllib.request

import pytest

from voice_assistant import models

URL = "https://huggingface.co/alon-p/models/model.onnx"


def serving(monkeypatch, served: dict[str, bytes], asked: list[str]):
    """HTTPS, stood in for: each address's bytes."""

    def urlopen(url, timeout):
        asked.append(url)
        return io.BytesIO(served[url])

    monkeypatch.setattr(models.urllib.request, "urlopen", urlopen)


def test_a_model_is_downloaded_once(tmp_path, monkeypatch):
    asked = []
    serving(monkeypatch, {URL: b"weights"}, asked)
    path = tmp_path / "models" / "model.onnx"
    assert models.fetch(path, URL, "a model").read_bytes() == b"weights"
    assert models.fetch(path, URL, "a model") == path and asked == [URL]  # there already: not asked for again


@pytest.mark.parametrize("url", ["http://example.com/model.onnx", "file:///etc/passwd", "ftp://x/model.onnx"])
def test_a_model_is_only_downloaded_over_https(tmp_path, url):
    with pytest.raises(ValueError, match="only downloaded over HTTPS"):
        models.fetch(tmp_path / "model.onnx", url, "a model")
    assert not (tmp_path / "model.onnx").exists()


def test_a_stalled_download_fails_instead_of_hanging_the_first_start(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DOWNLOAD_TIMEOUT_S", 0.2)
    server = socket.create_server(("127.0.0.1", 0))
    accepted = []
    threading.Thread(target=lambda: accepted.append(server.accept()), daemon=True).start()  # and never answers
    path = tmp_path / "model.onnx"
    failed = []
    local, real = f"http://127.0.0.1:{server.getsockname()[1]}/model.onnx", urllib.request.urlopen
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda url, timeout: real(local, timeout=timeout))

    def download():
        try:
            models.fetch(path, URL, "a model")
        except OSError as e:
            failed.append(e)

    downloading = threading.Thread(target=download, daemon=True)
    downloading.start()
    downloading.join(5)
    server.close()
    for connection, _ in accepted:
        connection.close()
    assert accepted and failed and not path.exists() and not path.with_suffix(".onnx.part").exists()
