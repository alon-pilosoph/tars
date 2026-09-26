"""The self-learning web UI: conversations and what TARS sent, the wakes to check, the voices, and the models.

    voice-assistant --web                 # http://127.0.0.1:8080, this machine only
    voice-assistant --web --host 0.0.0.0  # reachable from the home network (e.g. on the Pi)

It never leaves your network: no accounts, no cloud. Everything it shows and edits lives in voice_data/events/.
Only requests addressed to this machine by a home-network name are answered (see allowed_host), so a web page
somewhere else can't read or change anything through the browser of someone at home.
"""

import ipaddress
import threading
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .conversations import FILE, LIST, TARS, ConversationLog
from .events import NOT_PERSON, PERSON, UNKNOWN, EventLog

STATIC = Path(__file__).with_name("webui_static")  # the React app in webui/, built with `npm run build`
HIDDEN = {"embedding"}  # never sent to the browser
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class Label(BaseModel):
    label: Literal["real", "not_real"] | None  # None clears it


class Assignment(BaseModel):
    cluster_id: int | None


class ClusterName(BaseModel):
    name: str | None = None
    kind: Literal["person", "not_person", "unknown"] | None = None  # None: what the name implies

    @field_validator("name")
    @classmethod
    def _tidy(cls, name: str | None) -> str | None:
        name = " ".join(name.split()) if name else ""
        if len(name) > 60:
            raise ValueError("a name can be at most 60 characters")
        return name or None


class Merge(BaseModel):
    keep: int
    absorb: int


class Rating(BaseModel):
    rating: Literal["good", "bad"] | None  # None clears it


class Correction(BaseModel):
    text: str | None  # what they really said; None or "" clears it


class Tick(BaseModel):
    done: bool


class Seen(BaseModel):
    seen: bool = True


class UseVersion(BaseModel):
    version: str


class Models(Protocol):
    """The Models page: what hears the wake word, retraining the double-check, and its versions (retrain.py)."""

    def info(self) -> dict:
        """{"active": {...}, "history": [...], "last_retrain": {...} | None, "trainable": n}"""

    def retrain(self) -> dict:
        """Train a candidate and test it; it's put in use only if it's better. Returns how it went."""

    def use(self, version: str) -> None:
        """Put a version in use; KeyError if there's no such version."""


def allowed_host(host: str, extra: frozenset[str] = frozenset()) -> bool:
    """A name this machine is reached by at home: an IP address, localhost, a .local name, a single-label name
    (the Pi's hostname, a Tailscale MagicDNS name), *.ts.net, or one listed in [web] allowed_hosts. Anything else
    is a page elsewhere trying its luck through DNS rebinding."""
    name = (urlsplit(f"//{host}").hostname or "").lower()
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in extra or name in LOOPBACK or "." not in name or name.endswith((".local", ".ts.net"))


def same_site(origin: str, host: str, extra: frozenset[str] = frozenset()) -> bool:
    """A change may only come from this page itself (or the dev server on this machine)."""
    parts = urlsplit(origin)
    return parts.netloc == host or (parts.hostname or "") in LOOPBACK | extra


def create_app(
    log: EventLog, recluster=None, models: Models | None = None, allowed_hosts: frozenset[str] = frozenset()
) -> FastAPI:
    """`recluster` re-clusters the voices (see clustering.regroup); None hides the button.
    `models` is the Models page (retraining is manual, never scheduled); None when there's nothing to retrain.
    `allowed_hosts`: more names this machine is reached by, besides the ones allowed_host always accepts."""
    app = FastAPI(title="TARS")
    convos = ConversationLog(log)
    voices = threading.Lock()  # one change to the voices at a time: re-clustering mustn't undo a move made meanwhile
    retraining = threading.Lock()
    extra = frozenset(h.lower() for h in allowed_hosts)

    @app.middleware("http")
    async def only_from_home(request: Request, call_next):
        host = request.headers.get("host", "")
        if not allowed_host(host, extra):
            return PlainTextResponse(
                f"{host!r} isn't a name this machine answers to; add it to [web] allowed_hosts "
                "in config.toml if it should be",
                status_code=421,
            )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            cross = request.headers.get("sec-fetch-site") == "cross-site"
            if (origin is not None and not same_site(origin, host, extra)) or (origin is None and cross):
                return PlainTextResponse("changes can only come from the TARS page itself", status_code=403)
        return await call_next(request)

    def event_or_404(event_id: int) -> dict:
        event = log.get(event_id)
        if not event:
            raise HTTPException(404, "no such event")
        return event

    def cluster_or_404(cluster_id: int) -> dict:
        cluster = log.cluster(cluster_id)
        if not cluster:
            raise HTTPException(404, "no such voice")
        return cluster

    def turn_or_404(turn_id: int) -> dict:
        turn = convos.turn(turn_id)
        if not turn:
            raise HTTPException(404, "no such turn")
        return turn

    def item_or_404(item_id: int) -> dict:
        item = convos.item(item_id)
        if not item:
            raise HTTPException(404, "no such item")
        return item

    def file_or_404(rel: str | None) -> Path:
        path = (log.folder / rel).resolve() if rel else None
        if not path or not path.is_relative_to(log.folder.resolve()) or not path.exists():
            raise HTTPException(404, "no file")
        return path

    def public(item: dict) -> dict:
        """What the browser gets for an item: a sent file's URL (where it's stored stays on the server)."""
        if item["kind"] != FILE:
            return item
        url = f"/api/items/{item['id']}/file"
        return {**item, "url": url, "preview": url if (item["mime"] or "").startswith("image/") else None}

    def public_conversation(conv: dict) -> dict:
        conv = dict(conv)
        if "items" in conv:
            conv["items"] = [public(i) for i in conv["items"]]
        if "turns" in conv:
            conv["turns"] = [
                {**t, "items": [public(i) for i in t["items"]]} if "items" in t else t for t in conv["turns"]
            ]
        return conv

    if (STATIC / "index.html").exists():

        @app.get("/")
        def page():
            return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/assets", StaticFiles(directory=STATIC / "assets", check_dir=False), name="assets")
    else:

        @app.get("/")
        def page():
            return HTMLResponse(
                "<p>The TARS page isn't built. In webui/, run <code>npm ci && npm run build</code>. "
                "The API under /api works without it.</p>",
                status_code=503,
            )

    @app.get("/api/events")
    def list_events(kind: Literal["wake", "near_miss"] | None = None, limit: int = Query(500, ge=1, le=100_000)):
        return [{k: v for k, v in e.items() if k not in HIDDEN} for e in log.events(limit=limit, kind=kind)]

    # Before /api/audio/{event_id}/{which}, or "turn" would be taken for an event id.
    @app.get("/api/audio/turn/{turn_id}")
    def turn_audio(turn_id: int):
        return FileResponse(file_or_404(turn_or_404(turn_id)["audio"]), media_type="audio/wav")

    @app.get("/api/audio/{event_id}/{which}")
    def audio(event_id: int, which: Literal["wake", "request"]):
        column = {"wake": "audio", "request": "utterance_audio"}[which]
        return FileResponse(file_or_404(event_or_404(event_id)[column]), media_type="audio/wav")

    @app.post("/api/events/{event_id}/label")
    def label(event_id: int, body: Label):
        event_or_404(event_id)
        log.set_label(event_id, body.label)
        return {"ok": True}

    @app.post("/api/events/{event_id}/cluster")
    def retag(event_id: int, body: Assignment):
        event_or_404(event_id)
        with voices:
            if body.cluster_id is not None:
                cluster_or_404(body.cluster_id)
            log.assign(event_id, body.cluster_id, pinned=True)  # a person decided: re-clustering keeps it
        return {"ok": True}

    @app.delete("/api/events/{event_id}")
    def delete(event_id: int):
        event_or_404(event_id)
        log.delete(event_id)
        return {"ok": True}

    @app.get("/api/conversations")
    def conversations(limit: int = Query(200, ge=1, le=100_000), turns: bool = True):
        return [public_conversation(c) for c in convos.conversations(limit=limit, turns=turns)]

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: int):
        conv = convos.get(conversation_id)
        if not conv:
            raise HTTPException(404, "no such conversation")
        return public_conversation(conv)

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: int):
        if not convos.exists(conversation_id):
            raise HTTPException(404, "no such conversation")
        convos.delete(conversation_id)
        return {"ok": True}

    @app.post("/api/turns/{turn_id}/rating")
    def rate(turn_id: int, body: Rating):
        if turn_or_404(turn_id)["role"] != TARS:
            raise HTTPException(400, "only TARS's replies can be rated")
        convos.rate(turn_id, body.rating)
        return {"ok": True}

    @app.post("/api/turns/{turn_id}/correction")
    def correct(turn_id: int, body: Correction):
        if turn_or_404(turn_id)["role"] == TARS:
            raise HTTPException(400, "only what a person said can be corrected")
        convos.correct(turn_id, (body.text or "").strip() or None)
        return {"ok": True}

    @app.get("/api/items")
    def items(person: str | None = None, unseen: bool = False, limit: int = Query(500, ge=1, le=100_000)):
        return [public(i) for i in convos.items(person=person, unseen=unseen, limit=limit)]

    @app.post("/api/items/{item_id}/seen")
    def seen(item_id: int, body: Seen | None = None):
        item_or_404(item_id)
        convos.mark_seen(item_id, body.seen if body else True)  # {"seen": false} is Undo
        return {"ok": True}

    @app.post("/api/items/{item_id}/entries/{index}")
    def tick(item_id: int, index: int, body: Tick):
        if item_or_404(item_id)["kind"] != LIST:
            raise HTTPException(400, "only lists have entries")
        try:
            convos.tick(item_id, index, body.done)
        except IndexError:
            raise HTTPException(404, "no such entry") from None
        return {"ok": True}

    @app.delete("/api/items/{item_id}")
    def delete_item(item_id: int):
        item_or_404(item_id)
        convos.delete_item(item_id)
        return {"ok": True}

    @app.get("/api/items/{item_id}/file")
    def item_file(item_id: int):
        item = item_or_404(item_id)
        # Always a download (never rendered as a page on this origin); images still preview in an <img>.
        return FileResponse(
            file_or_404(convos.item_file(item_id)),
            filename=item.get("name"),
            media_type=item.get("mime") or "application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/clusters")
    def clusters():
        return log.clusters()

    @app.post("/api/clusters")
    def new_cluster(body: ClusterName):
        with voices:
            return {"id": log.new_cluster(body.name, body.kind or (PERSON if body.name else UNKNOWN))}

    # Before /api/clusters/{cluster_id}, or "merge" would be taken for a cluster id.
    @app.post("/api/clusters/merge")
    def merge(body: Merge):
        if body.keep == body.absorb:
            raise HTTPException(400, "pick two different voices")
        with voices:
            cluster_or_404(body.keep)
            cluster_or_404(body.absorb)
            log.merge_clusters(body.keep, body.absorb)
        return {"ok": True}

    @app.post("/api/clusters/{cluster_id}")
    def rename(cluster_id: int, body: ClusterName):
        with voices:
            current = cluster_or_404(cluster_id)["kind"]
            # Naming the TV "TV" keeps it "not a person" unless the page says otherwise.
            kind = body.kind or (NOT_PERSON if current == NOT_PERSON else PERSON if body.name else UNKNOWN)
            log.rename_cluster(cluster_id, body.name, kind)
        return {"ok": True}

    @app.post("/api/recluster")
    def run_recluster():
        if recluster is None:
            raise HTTPException(501, "clustering isn't set up")
        with voices:
            return recluster()

    @app.get("/api/models")
    def models_page():
        return models.info() if models else {"active": {}, "history": [], "last_retrain": None, "trainable": 0}

    @app.post("/api/retrain")
    def retrain():
        if models is None:
            raise HTTPException(501, "the double-check has no learned layer to retrain")
        if not retraining.acquire(blocking=False):
            raise HTTPException(409, "already retraining")
        try:
            return models.retrain()
        finally:
            retraining.release()

    @app.post("/api/models/use")
    def use_version(body: UseVersion):
        if models is None:
            raise HTTPException(501, "the double-check has no learned layer to switch")
        try:
            models.use(body.version)
        except KeyError:
            raise HTTPException(404, "no such version") from None
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return {**log.counts(), "clustering": recluster is not None, "unseen_items": convos.unseen_count()}

    return app


def serve(
    log: EventLog,
    host: str,
    port: int,
    recluster=None,
    models: Models | None = None,
    allowed_hosts: frozenset[str] = frozenset(),
) -> None:
    import uvicorn

    print(f"TARS web UI on http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(log, recluster, models, allowed_hosts), host=host, port=port, log_level="warning")
