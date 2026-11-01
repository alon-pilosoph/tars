"""A line of JSON each way between TARS's processes on one machine, over a Unix socket in the event log's folder.

The assistant holds the speakerphone, so another process that needs it (the web UI, for a recording session) asks the
assistant: `{"op": "record"}` and so on (Assistant.control_ops). One request a connection, one answer. The socket is
the folder's, readable only by its user, so nothing off this machine, or of another user's, reaches it.
"""

import json
import os
import socket
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path

SOCKET = "assistant.sock"  # in the event log's folder
MOST_BYTES = 2_000_000  # an answer can carry a take: 15 s of 16 kHz audio is under a megabyte in base64
TIMEOUT_S = 10.0

Op = Callable[[dict], dict]


class Unavailable(ConnectionError):
    """The assistant isn't running, or didn't answer in time."""


class Refused(RuntimeError):
    """The assistant answered no: `why` is a word the asker turns into words for people (busy, muted, gone)."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


class Server:
    """Serves `ops` on the folder's socket from a thread of its own, until close()."""

    def __init__(self, folder: Path, ops: dict[str, Op]):
        self.path = folder / SOCKET
        self.path.unlink(missing_ok=True)  # left by an assistant that didn't close it
        ops_ = ops

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    request = json.loads(self.rfile.readline(MOST_BYTES))
                    op = ops_.get(request.get("op"))
                    answer = op(request) if op else {"ok": False, "why": "unknown"}
                except Refused as e:
                    answer = {"ok": False, "why": e.why}
                except Exception as e:  # noqa: BLE001 - one bad request mustn't stop the server
                    print(f"(control: {e!r})")
                    answer = {"ok": False, "why": "error"}
                self.wfile.write(json.dumps(answer).encode() + b"\n")

        old = os.umask(0o177)  # the socket is made readable and writable by this user only
        try:
            self._server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        finally:
            os.umask(old)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="control", daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self.path.unlink(missing_ok=True)


def ask(folder: Path, op: str, timeout_s: float = TIMEOUT_S, **args) -> dict:
    """The assistant's answer to `op`. Raises Unavailable if nothing answers, Refused if it says no."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect(str(folder / SOCKET))
            s.sendall(json.dumps({"op": op, **args}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n") and len(data) < MOST_BYTES:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as e:  # no socket, nobody listening, or no answer in time
        raise Unavailable(str(e)) from e
    try:
        answer = json.loads(data)
    except ValueError as e:
        raise Unavailable("the assistant's answer was cut short") from e
    if not answer.get("ok", True):
        raise Refused(answer.get("why", "error"))
    return answer
