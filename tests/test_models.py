"""The small models downloaded on first use: written whole, or not at all, only if they're the expected file, and
never waited on forever."""

import email.message
import hashlib
import io
import socket
import threading
import urllib.error
import urllib.request

import pytest

from voice_assistant import models

URL = "https://huggingface.co/alon-p/models/model.onnx"
WEIGHTS = hashlib.sha256(b"weights").hexdigest()


def serving(monkeypatch, served: dict[str, bytes], asked: list[str]):
    """HTTPS, stood in for: each address's bytes."""

    class Opener:
        def open(self, url, timeout):
            asked.append(url)
            return io.BytesIO(served[url])

    monkeypatch.setattr(models, "_opener", Opener())


def test_a_model_is_downloaded_once(tmp_path, monkeypatch):
    asked = []
    serving(monkeypatch, {URL: b"weights"}, asked)
    path = tmp_path / "models" / "model.onnx"
    assert models.fetch(path, URL, "a model", WEIGHTS).read_bytes() == b"weights"
    assert models.fetch(path, URL, "a model", WEIGHTS) == path and asked == [URL]  # there already: not asked for again


def test_a_model_that_isnt_the_expected_file_is_not_kept(tmp_path, monkeypatch):
    serving(monkeypatch, {URL: b"something else"}, [])
    path = tmp_path / "model.onnx"
    with pytest.raises(ValueError, match="isn't the expected file"):
        models.fetch(path, URL, "a model", WEIGHTS)
    assert not path.exists() and not path.with_suffix(".onnx.part").exists()


@pytest.mark.parametrize("to", ["http://example.com/model.onnx", "ftp://x/model.onnx"])
def test_a_download_redirected_off_https_is_refused(to):
    headers = email.message.Message()
    headers["Location"] = to
    request = urllib.request.Request(URL)
    with pytest.raises(urllib.error.URLError, match="redirected off HTTPS"):
        models.HttpsOnlyRedirects().redirect_request(request, None, 302, "Found", headers, to)
    assert models.HttpsOnlyRedirects().redirect_request(request, None, 302, "Found", headers, URL + "?cdn").full_url


@pytest.mark.parametrize("url", ["http://example.com/model.onnx", "file:///etc/passwd", "ftp://x/model.onnx"])
def test_a_model_is_only_downloaded_over_https(tmp_path, url):
    with pytest.raises(ValueError, match="only downloaded over HTTPS"):
        models.fetch(tmp_path / "model.onnx", url, "a model", WEIGHTS)
    assert not (tmp_path / "model.onnx").exists()


def test_a_stalled_download_fails_instead_of_hanging_the_first_start(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DOWNLOAD_TIMEOUT_S", 0.2)
    server = socket.create_server(("127.0.0.1", 0))
    accepted = []
    threading.Thread(target=lambda: accepted.append(server.accept()), daemon=True).start()  # and never answers
    path = tmp_path / "model.onnx"
    failed = []
    local = f"http://127.0.0.1:{server.getsockname()[1]}/model.onnx"

    class Opener:
        def open(self, url, timeout):
            return urllib.request.urlopen(local, timeout=timeout)

    monkeypatch.setattr(models, "_opener", Opener())

    def download():
        try:
            models.fetch(path, URL, "a model", WEIGHTS)
        except OSError as e:
            failed.append(e)

    downloading = threading.Thread(target=download, daemon=True)
    downloading.start()
    downloading.join(5)
    server.close()
    for connection, _ in accepted:
        connection.close()
    assert accepted and failed and not path.exists() and not path.with_suffix(".onnx.part").exists()
