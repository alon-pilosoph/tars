"""The self-learning web UI: conversations and what TARS sent, the wakes to check, the voices, and the models.

    voice-assistant --web                 # http://127.0.0.1:8080, this machine only
    voice-assistant --web --host 0.0.0.0  # reachable from the home network (e.g. on the Pi)

No accounts, no cloud: everything it shows and edits lives in voice_data/events/. Only requests addressed to this
machine by a home-network name are answered (see allowed_host), so a page elsewhere can't read or change anything
through the browser of someone at home.
"""

import ipaddress
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .clustering import MIN_REQUESTS_TO_ENROLL
from .conversations import FILE, LIST, ROLE_TARS, ConversationLog
from .events import NOT_PERSON, NOT_REAL, PERSON, REAL, UNKNOWN, EventLog
from .reminders import MESSAGE, REMINDER, TIMER, WEB, NewReminder, Reminders, line
from .versions import INSTALLED, FixedPair, ModelVersions

STATIC = Path(__file__).with_name("webui_static")  # the React app in webui/, built with `npm run build`
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class Label(BaseModel):
    label: Literal[REAL, NOT_REAL] | None  # None clears it


class Assignment(BaseModel):
    cluster_id: int | None


class ClusterName(BaseModel):
    name: str | None = None
    kind: Literal[PERSON, NOT_PERSON, UNKNOWN] | None = None  # None: what the name implies

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
    text: str | None  # None or "" clears it


class Tick(BaseModel):
    done: bool


class UseVersion(BaseModel):
    version: str


class ReminderBody(BaseModel):
    kind: Literal[TIMER, REMINDER, MESSAGE]
    text: str | None = None
    for_name: str | None = None
    from_name: str | None = None
    due: float | None = None  # Unix time; None with when_back
    when_back: bool = False
    needs_ack: bool = True
    repeat_every_min: float | None = None  # None: [reminders]'s
    max_tries: int | None = None


class Snooze(BaseModel):
    minutes: float


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


def event(e: dict) -> dict:
    """What the browser gets for an event: whether it has audio, not where it's stored."""
    out = {
        k: e[k]
        for k in (
            "id",
            "ts",
            "kind",
            "outcome",
            "wake_score",
            "heard",
            "confidence",
            "transcript",
            "follow",
            "cluster_id",
            "label",
            "auto_label",
            "auto_reason",
        )
    }
    out.update(
        cluster_pinned=bool(e["cluster_pinned"]),
        has_wake_audio=bool(e["audio"]),
        has_request_audio=bool(e["utterance_audio"]),
    )
    return out


def create_app(
    log: EventLog,
    recluster: Callable[[], dict] | None = None,
    pairs: ModelVersions | FixedPair | None = None,
    allowed_hosts: frozenset[str] = frozenset(),
    reminders: Reminders | None = None,
    voice_names: Callable[[], list[str]] = list,
) -> FastAPI:
    """`recluster` regroups the voices (see clustering.regroup); None hides the button.
    `pairs`: the wake models TARS listens with, for the Models page (versions.pair_source).
    `allowed_hosts`: more names this machine is reached by, besides the ones allowed_host always accepts.
    `reminders`: TARS's timers, reminders and messages; None when they're off. `voice_names`: the names TARS knows
    by voice (speaker ID's voiceprints), which a message waiting until someone is back needs."""
    app = FastAPI(title="TARS")
    convos = ConversationLog(log)
    voices = threading.Lock()  # one change to the voices at a time: re-clustering mustn't undo a move made meanwhile
    extra = frozenset(h.lower() for h in allowed_hosts)

    @app.middleware("http")
    async def only_from_home(request: Request, call_next):
        host = request.headers.get("host", "")
        if not allowed_host(host, extra):
            return JSONResponse(
                {
                    "detail": f"{host!r} isn't a name this machine answers to; add it to [web] "
                    "allowed_hosts in config.toml if it should be"
                },
                status_code=421,
            )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            cross = request.headers.get("sec-fetch-site") == "cross-site"
            if (origin is not None and not same_site(origin, host, extra)) or (origin is None and cross):
                return JSONResponse({"detail": "changes can only come from the TARS page itself"}, status_code=403)
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

    def reminders_or_501() -> Reminders:
        if reminders is None:
            raise HTTPException(501, "reminders are off ([reminders] enabled in config.toml)")
        return reminders

    def reminder_or_404(reminder_id: int) -> dict:
        r = reminders_or_501().get(reminder_id)
        if not r:
            raise HTTPException(404, "no such reminder")
        return r

    def public_reminder(r: dict) -> dict:
        return {**r, "needs_ack": bool(r["needs_ack"]), "says": line(r)}

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
        return {
            **conv,
            "turns": [{**t, "items": [public(i) for i in t["items"]]} if "items" in t else t for t in conv["turns"]],
        }

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
    def list_events(limit: int | None = Query(None, ge=1)):
        """Without a `limit`: every event still waiting for an answer, and the newest answered ones."""
        return [event(e) for e in (log.events(limit) if limit else log.for_review())]

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
            log.assign(event_id, body.cluster_id, pinned=True)
        return {"ok": True}

    @app.delete("/api/events/{event_id}")
    def delete(event_id: int):
        event_or_404(event_id)
        log.delete(event_id)
        return {"ok": True}

    @app.get("/api/conversations")
    def conversations():
        return [public_conversation(c) for c in convos.conversations()]

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
        if turn_or_404(turn_id)["role"] != ROLE_TARS:
            raise HTTPException(400, "only TARS's replies can be rated")
        convos.rate(turn_id, body.rating)
        return {"ok": True}

    @app.post("/api/turns/{turn_id}/correction")
    def correct(turn_id: int, body: Correction):
        if turn_or_404(turn_id)["role"] == ROLE_TARS:
            raise HTTPException(400, "only what a person said can be corrected")
        convos.correct(turn_id, (body.text or "").strip() or None)
        return {"ok": True}

    @app.get("/api/items")
    def items():
        return [public(i) for i in convos.items()]

    @app.post("/api/items/{item_id}/seen")
    def seen(item_id: int):
        item_or_404(item_id)
        convos.mark_seen(item_id)
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
            file_or_404(item["file"]),
            filename=item["file_name"],
            media_type=item["mime"] or "application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/clusters")
    def clusters():
        samples = log.samples()
        return [{**c, "samples": samples.get(c["id"], [])} for c in log.clusters()]

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
    def models():
        if pairs is None:
            return {"active": None, "results": None, "history": [], "problem": None, "learning": log.learning()}
        pair, history, problem = pairs.overview() if isinstance(pairs, ModelVersions) else (pairs.in_use(), [], None)
        return {
            "active": {
                "version": pair.version,
                "wake_model": pair.wake_model,
                "threshold": pair.threshold,
                "check_model": pair.check_model or None,
                "check_window_s": pair.check_window_s,
                "replaced": pair.replaced,
            },
            "results": pair.results,
            "history": history,
            "problem": problem,
            "learning": log.learning(since=None if pair.version == INSTALLED else pair.ts),
        }

    @app.post("/api/models/use")
    def use_version(body: UseVersion):
        if not isinstance(pairs, ModelVersions):
            raise HTTPException(404, "no such version")
        try:
            pairs.use(body.version)
        except KeyError:
            raise HTTPException(404, "no such version, or it can't be loaded") from None
        return {"ok": True}

    @app.get("/api/reminders")
    def list_reminders():
        if reminders is None:
            return {"enabled": False, "reminders": [], "voices": [], "defaults": None}
        return {
            "enabled": True,
            "reminders": [public_reminder(r) for r in reminders.all()],
            "voices": voice_names(),
            "defaults": {
                "repeat_every_min": reminders.repeat_every_s / 60,
                "max_tries": reminders.max_tries,
                "timer_ring_min": reminders.timer_ring_s / 60,
            },
        }

    @app.post("/api/reminders")
    def new_reminder(body: ReminderBody):
        if body.when_back == (body.due is not None):
            raise HTTPException(400, "give a time, or wait until they're back, not both")
        repeat = body.repeat_every_min * 60 if body.repeat_every_min is not None else None
        new = NewReminder(
            body.kind, body.text, body.for_name, body.from_name, body.due, body.needs_ack, repeat, body.max_tries
        )
        try:
            return {"id": reminders_or_501().add(new, WEB, voices=voice_names())}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/reminders/{reminder_id}/ack")
    def ack_reminder(reminder_id: int):
        reminder_or_404(reminder_id)
        if not reminders_or_501().ack(reminder_id, None, WEB):
            raise HTTPException(409, "it's over already")
        return {"ok": True}

    @app.post("/api/reminders/{reminder_id}/snooze")
    def snooze_reminder(reminder_id: int, body: Snooze):
        reminder_or_404(reminder_id)
        try:
            snoozed = reminders_or_501().snooze(reminder_id, body.minutes)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if not snoozed:
            raise HTTPException(409, "it's over already")
        return {"ok": True}

    @app.post("/api/reminders/{reminder_id}/cancel")
    def cancel_reminder(reminder_id: int):
        reminder_or_404(reminder_id)
        if not reminders_or_501().cancel(reminder_id):
            raise HTTPException(409, "it's over already")
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return {"clustering": recluster is not None, "enroll_at": MIN_REQUESTS_TO_ENROLL}

    return app


def serve(log: EventLog, host: str, port: int, **options) -> None:
    import uvicorn

    print(f"TARS web UI on http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(log, **options), host=host, port=port, log_level="warning")
