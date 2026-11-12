"""Signing in to the web UI with a passkey ([web] sign_in, docs/web-ui.md#signing-in).

Each household member has one or more passkeys (WebAuthn: a key pair made and kept by their phone or computer, which
signs a fresh challenge to sign in; nothing to type, nothing a phishing page can reuse). The first one is made from an
invite: `voice-assistant --web-invite Stacey` makes a one-time link, sent to her Telegram chat, that's good for a day.
Signing in sets a session cookie: HttpOnly (no script reads it), SameSite=Strict (no other site's page sends it), and
Secure over HTTPS. With sign-in on, every route needs a session but the sign-in pages, the page's shell and its files,
and recording sessions, whose links carry their own token (`OPEN`); a test walks every route to keep it so.

Browsers make passkeys only over HTTPS, or on localhost: [web] tls_cert and tls_key serve the web UI over HTTPS (a
Tailscale certificate: `tailscale cert`). A passkey belongs to the name the page was opened by, so one made at
homepi.tailnet.ts.net signs in there only.
"""

import base64
import hashlib
import html
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .events import person_key
from .store import Store

COOKIE = "tars_session"
SESSION_S = 30 * 86400.0
INVITE_S = 86400.0
CEREMONY_S = 300.0  # how long a challenge waits for its passkey
MOST_CEREMONIES = 1000  # waiting at once; past it the oldest goes, so a flood of requests can't fill the memory
# What's reachable without a session: the sign-in pages and their API, the page's shell and files (which hold no
# data), and recording sessions (/api/session/<token>/...), whose links carry their own token.
OPEN_UNDER = ("/sign-in", "/api/sign-in")  # these and the paths below them
OPEN = ("/api/session/", "/assets/")
OPEN_EXACT = ("/", "/favicon.ico")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_id(person: str) -> bytes:
    """The id a passkey keeps for its person, and hands back when it signs in (its userHandle)."""
    return hashlib.sha256(f"tars:{person_key(person)}".encode()).digest()[:16]


def shown(person: str) -> str:
    """A name as the web UI shows it: "stacey" -> "Stacey", "mary ann" -> "Mary Ann" (format.ts personName)."""
    return " ".join(w[0].upper() + w[1:] for w in person.split())


class Accounts:
    """Invites, passkeys and sessions, in the event database (web_invites, passkeys, web_sessions). People are kept
    as person_key has them."""

    def __init__(self, store: Store, clock=time.time):
        self.store, self._clock = store, clock

    def invite(self, person: str) -> str:
        """A one-time token for `person` to make a passkey with, good for INVITE_S."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self.store.transaction() as db:
            _purge(db, now)
            db.execute(
                "INSERT INTO web_invites (token, person, expires) VALUES (?, ?, ?)",
                (_hash(token), person_key(person), now + INVITE_S),
            )
        return token

    def invited(self, token: str) -> str | None:
        """Whose invite it is, while it's unused and fresh."""
        row = self.store.row(
            "SELECT person FROM web_invites WHERE token=? AND used IS NULL AND expires > ?",
            (_hash(token), self._clock()),
        )
        return row["person"] if row else None

    def add_passkey(self, token: str, credential_id: str, public_key: bytes, sign_count: int, agent: str) -> str | None:
        """Keeps a passkey made from an invite, and uses the invite up. Whose it is; None if the invite's gone."""
        now = self._clock()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT person FROM web_invites WHERE token=? AND used IS NULL AND expires > ?", (_hash(token), now)
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE web_invites SET used=? WHERE token=?", (now, _hash(token)))
            db.execute(
                "INSERT INTO passkeys (id, person, public_key, sign_count, created, device) VALUES (?, ?, ?, ?, ?, ?)",
                (credential_id, row["person"], public_key, sign_count, now, agent[:200]),
            )
            return row["person"]

    def passkey(self, credential_id: str) -> dict | None:
        return self.store.row("SELECT * FROM passkeys WHERE id=?", (credential_id,))

    def passkeys(self, person: str) -> list[dict]:
        return self.store.rows("SELECT id FROM passkeys WHERE person=?", (person_key(person),))

    def used(self, credential_id: str, sign_count: int) -> None:
        self.store.write(
            "UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?", (sign_count, self._clock(), credential_id)
        )

    def start_session(self, person: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self.store.transaction() as db:
            _purge(db, now)
            db.execute(
                "INSERT INTO web_sessions (token, person, created, expires) VALUES (?, ?, ?, ?)",
                (_hash(token), person_key(person), now, now + SESSION_S),
            )
        return token

    def session(self, token: str | None) -> str | None:
        """Whose session it is, while it's fresh."""
        if not token:
            return None
        row = self.store.row(
            "SELECT person FROM web_sessions WHERE token=? AND expires > ?", (_hash(token), self._clock())
        )
        return row["person"] if row else None

    def end_session(self, token: str | None) -> None:
        if token:
            self.store.write("DELETE FROM web_sessions WHERE token=?", (_hash(token),))

    def remove(self, person: str) -> tuple[int, int, int]:
        """Signs `person` out everywhere, forgets their passkeys and voids their unused invites. How many passkeys,
        sessions and invites went."""
        key = person_key(person)
        with self.store.transaction() as db:
            return (
                db.execute("DELETE FROM passkeys WHERE person=?", (key,)).rowcount,
                db.execute("DELETE FROM web_sessions WHERE person=?", (key,)).rowcount,
                db.execute("DELETE FROM web_invites WHERE person=? AND used IS NULL", (key,)).rowcount,
            )


def _purge(db, now: float) -> None:
    """Old sessions and invites, which nothing would ever let in again."""
    db.execute("DELETE FROM web_sessions WHERE expires <= ?", (now,))
    db.execute("DELETE FROM web_invites WHERE expires <= ?", (now,))


@dataclass
class Ceremony:
    challenge: bytes
    expires: float
    invite: str | None = None


class Ceremonies:
    """Challenges handed out and waiting for a passkey's answer, by an opaque id; each is taken once."""

    def __init__(self, clock=time.time):
        self._waiting: dict[str, Ceremony] = {}
        self._lock, self._clock = threading.Lock(), clock

    def start(self, challenge: bytes, invite: str | None = None) -> str:
        cid = secrets.token_urlsafe(16)
        with self._lock:
            now = self._clock()
            self._waiting = {k: c for k, c in self._waiting.items() if c.expires > now}
            while len(self._waiting) >= MOST_CEREMONIES:
                del self._waiting[next(iter(self._waiting))]
            self._waiting[cid] = Ceremony(challenge, now + CEREMONY_S, invite)
        return cid

    def take(self, cid: str) -> Ceremony | None:
        with self._lock:
            c = self._waiting.pop(cid, None)
        return c if c and c.expires > self._clock() else None


class Answer(BaseModel):
    ceremony: str
    credential: dict


def is_open(path: str) -> bool:
    return path in OPEN_EXACT or path.startswith(OPEN) or any(path == p or path.startswith(p + "/") for p in OPEN_UNDER)


def _where(request: Request) -> tuple[str, str]:
    """The relying party (the name the page was opened by) and the origin a passkey's answer must come from."""
    host = request.headers.get("host", "")
    return host.rsplit(":", 1)[0] if not host.endswith("]") else host, f"{request.url.scheme}://{host}"


def add_sign_in(app: FastAPI, accounts: Accounts, clock=time.time) -> None:
    """The sign-in pages and API, and the rule that everything else needs a session."""
    import webauthn
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    ceremonies = Ceremonies(clock)

    # HTTP only: websockets never pass through it, so a websocket route needs a check of its own (there are none;
    # tests/test_signin.py fails on one that lets anyone in).
    @app.middleware("http")
    async def signed_in(request: Request, call_next):
        if is_open(request.url.path) or await run_in_threadpool(accounts.session, request.cookies.get(COOKIE)):
            return await call_next(request)
        return JSONResponse({"detail": "sign in first"}, status_code=401)

    def signed(response: Response, request: Request, person: str) -> Response:
        response.set_cookie(
            COOKIE,
            accounts.start_session(person),
            max_age=int(SESSION_S),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.get("/sign-in", include_in_schema=False)
    def sign_in_page():
        return HTMLResponse(_page("Sign in to TARS", "", "Sign in with a passkey", "sign-in"))

    @app.get("/sign-in/invite/{token}", include_in_schema=False)
    def invite_page(token: str):
        person = accounts.invited(token)
        if person is None:
            return HTMLResponse(_page("This link has been used", "Ask for a new one.", "", ""), status_code=410)
        return HTMLResponse(
            _page(
                f"Welcome, {html.escape(shown(person))}",
                "Make a passkey on this device to sign in to TARS.",
                "Make a passkey",
                "invite",
            )
        )

    @app.post("/api/sign-in/options")
    def sign_in_options(request: Request):
        rp_id, _ = _where(request)
        options = webauthn.generate_authentication_options(
            rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED
        )
        return {"ceremony": ceremonies.start(options.challenge), "options": json.loads(options_to_json(options))}

    @app.post("/api/sign-in")
    def sign_in(answer: Answer, request: Request):
        ceremony = ceremonies.take(answer.ceremony)
        key = accounts.passkey(str(answer.credential.get("id", "")))
        if ceremony is None or ceremony.invite is not None or key is None:
            raise HTTPException(401, "that passkey isn't one TARS knows, or it took too long: try again")
        rp_id, origin = _where(request)
        try:
            done = webauthn.verify_authentication_response(
                credential=answer.credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=key["public_key"],
                credential_current_sign_count=key["sign_count"],
                require_user_verification=True,
            )
        except Exception:  # noqa: BLE001 - any failed check is the same refusal
            raise HTTPException(401, "that passkey didn't check out: try again") from None
        handle = (answer.credential.get("response") or {}).get("userHandle")
        if handle and _unb64(handle) != user_id(key["person"]):
            raise HTTPException(401, "that passkey didn't check out: try again")
        accounts.used(key["id"], done.new_sign_count)
        return signed(JSONResponse({"person": key["person"]}), request, key["person"])

    @app.post("/api/sign-in/invite/{token}/options")
    def invite_options(token: str, request: Request):
        person = accounts.invited(token)
        if person is None:
            raise HTTPException(410, "this link has been used, or it's too old: ask for a new one")
        rp_id, _ = _where(request)
        options = webauthn.generate_registration_options(
            rp_id=rp_id,
            rp_name="TARS",
            user_name=person,
            user_id=user_id(person),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"])) for k in accounts.passkeys(person)
            ],
        )
        return {
            "ceremony": ceremonies.start(options.challenge, invite=token),
            "options": json.loads(options_to_json(options)),
        }

    @app.post("/api/sign-in/invite/{token}")
    def invite(token: str, answer: Answer, request: Request):
        ceremony = ceremonies.take(answer.ceremony)
        if ceremony is None or ceremony.invite != token:
            raise HTTPException(401, "that took too long: try again")
        rp_id, origin = _where(request)
        try:
            made = webauthn.verify_registration_response(
                credential=answer.credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
                require_user_verification=True,
            )
        except Exception:  # noqa: BLE001 - any failed check is the same refusal
            raise HTTPException(400, "the passkey couldn't be made: try again") from None
        person = accounts.add_passkey(
            token,
            bytes_to_base64url(made.credential_id),
            made.credential_public_key,
            made.sign_count,
            request.headers.get("user-agent", ""),
        )
        if person is None:
            raise HTTPException(410, "this link has been used, or it's too old: ask for a new one")
        return signed(JSONResponse({"person": person}), request, person)

    @app.post("/api/sign-out")
    def sign_out(request: Request):
        accounts.end_session(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/me")
    def me(request: Request):
        return {"person": accounts.session(request.cookies.get(COOKIE))}


def _unb64(s: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except ValueError, TypeError:
        return None


def _page(title: str, lead: str, button: str, kind: str) -> str:
    """The sign-in page: one button, and a few lines of script for the passkey."""
    script = (Path(__file__).parent / "signin.js").read_text() if kind else ""
    action = f'<button id="go" data-kind="{kind}">{button}</button><p id="said" role="status"></p>' if kind else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>{title}</title>
<style>
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0e1012; color: #e8e6e1;
  font: 16px/1.5 system-ui, -apple-system, sans-serif; }}
main {{ width: min(22rem, 100% - 3rem); }}
h1 {{ font-size: 1.4rem; font-weight: 600; margin: 0 0 .4rem; }}
p {{ color: #a7a49c; margin: 0 0 1.4rem; }}
.k {{ letter-spacing: .14em; text-transform: uppercase; font-size: .72rem; color: #8a8780; margin-bottom: 1.2rem; }}
button {{ width: 100%; padding: .85rem 1rem; border: 0; border-radius: .6rem; background: #e8e6e1; color: #0e1012;
  font: inherit; font-weight: 600; cursor: pointer; }}
button:disabled {{ opacity: .5; cursor: default; }}
#said {{ margin-top: 1rem; min-height: 1.5em; }}
</style></head>
<body><main><div class="k">TARS</div><h1>{title}</h1><p>{lead}</p>{action}</main>
<script>{script}</script></body></html>"""
