"""Signing in to the web UI with a passkey (signin.py): invites, the WebAuthn ceremonies against a software
authenticator, sessions, and every route closed without one."""

import base64
import hashlib
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from starlette.routing import Host, Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from voice_assistant.events import EventLog
from voice_assistant.signin import COOKIE, INVITE_S, MOST_CEREMONIES, SESSION_S, Accounts, Ceremonies, is_open
from voice_assistant.webui import create_app

HOST = "homepi.local"
ORIGIN = f"http://{HOST}"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    """A passkey on a phone, in software: makes a P-256 key for a site and signs its challenges, as WebAuthn says."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = b"passkey-" + bytes(range(8))
        self.count = 0

    def _auth_data(self, rp_id: str, attested: bytes = b"") -> bytes:
        self.count += 1
        flags = 0x01 | 0x04 | (0x40 if attested else 0)  # user present, verified, credential data
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + attested

    def create(self, options: dict, origin: str = ORIGIN) -> dict:
        numbers = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        attested = bytes(16) + struct.pack(">H", len(self.id)) + self.id + cbor2.dumps(cose)
        data = self._auth_data(options["rp"]["id"], attested)
        client = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin}).encode()
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})
        return {
            "id": b64(self.id),
            "rawId": b64(self.id),
            "type": "public-key",
            "response": {"clientDataJSON": b64(client), "attestationObject": b64(attestation)},
            "clientExtensionResults": {},
        }

    def get(self, options: dict, origin: str = ORIGIN, handle: bytes | None = None) -> dict:
        data = self._auth_data(options["rpId"])
        client = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}).encode()
        signature = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64(self.id),
            "rawId": b64(self.id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client),
                "authenticatorData": b64(data),
                "signature": b64(signature),
                "userHandle": b64(handle) if handle is not None else None,
            },
            "clientExtensionResults": {},
        }


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def site(tmp_path):
    log = EventLog(tmp_path / "events")
    clock = Clock()
    accounts = Accounts(log.store, clock=clock)
    client = TestClient(create_app(log, accounts=accounts), base_url=ORIGIN, headers={"Origin": ORIGIN})
    return client, accounts, clock, log


def make_passkey(client, accounts, phone: Authenticator, person="Stacey"):
    token = accounts.invite(person)
    asked = client.post(f"/api/sign-in/invite/{token}/options").json()
    return token, client.post(
        f"/api/sign-in/invite/{token}",
        json={"ceremony": asked["ceremony"], "credential": phone.create(asked["options"])},
    )


def sign_in(client, phone: Authenticator, origin: str = ORIGIN, handle: bytes | None = None):
    asked = client.post("/api/sign-in/options").json()
    return client.post(
        "/api/sign-in", json={"ceremony": asked["ceremony"], "credential": phone.get(asked["options"], origin, handle)}
    )


# Every route that answers without a session; a new one has to be added here on purpose.
OPEN_ROUTES = {
    "/",
    "/sign-in",
    "/sign-in/invite/{token}",
    "/api/sign-in",
    "/api/sign-in/options",
    "/api/sign-in/invite/{token}",
    "/api/sign-in/invite/{token}/options",
    "/assets",
}


IDS = {"event_id", "turn_id", "item_id", "cluster_id", "reminder_id", "skill_id", "preference_id", "conversation_id",
       "index", "take_id"}  # fmt: skip


def concrete(route) -> str:
    """A path the route answers: its ids 1, its other parameters their own names."""
    path = route.path_format.replace("{", "").replace("}", "")
    return "/".join("1" if part in IDS else part for part in path.split("/"))


def walk(client) -> tuple[list[str], set[str]]:
    """Asks every route of the app without a session: the paths refused, and the open routes that answered. Fails on
    a route that answers but isn't open; on a websocket that lets anyone in, since the sign-in check is HTTP
    middleware, which websockets never pass through; and on a kind of route it doesn't know, so none goes unasked."""
    shut, opened = [], set()
    for route in client.app.routes:
        if isinstance(route, WebSocketRoute):  # FastAPI's APIWebSocketRoute too
            let_in = False
            try:
                with client.websocket_connect(concrete(route)):
                    let_in = True
            except WebSocketDisconnect:
                pass  # closed before it was accepted, or once it had been
            if let_in:
                pytest.fail(f"websocket {route.path} lets anyone in: guard it, the sign-in middleware can't")
            shut.append(route.path)
            continue
        if isinstance(route, Mount):
            path, methods = f"{route.path}/x.js", {"GET"}
        elif isinstance(route, Route):  # FastAPI's APIRoute, and Starlette's own: the docs and /openapi.json
            path, methods = concrete(route), route.methods or {"GET"}
        else:
            pytest.fail(f"{route!r}: a kind of route this walk can't ask; teach it, so the route can't go unchecked")
        for method in methods - {"HEAD"}:
            r = client.request(method, path)
            if route.path in OPEN_ROUTES or route.path.startswith("/api/session/"):  # recording sessions' own token
                opened.add(route.path)
                assert r.status_code != 401 or path.startswith("/api/sign-in"), (method, path)
            else:
                assert r.status_code == 401, (method, path, r.status_code)
                shut.append(path)
    return shut, opened


def test_without_a_session_every_route_but_the_open_ones_is_refused(site):
    client, _, _, _ = site
    shut, opened = walk(client)
    assert {"/api/events", "/api/conversations/1", "/api/audio/1/which", "/api/status", "/api/me"} <= set(shut)
    assert {"/openapi.json", "/docs", "/redoc"} <= set(shut)  # FastAPI's own pages, which aren't APIRoutes
    assert OPEN_ROUTES <= opened


def test_the_walk_fails_on_a_websocket_anyone_can_open_and_passes_a_guarded_one(site):
    client, _, _, _ = site

    async def refuses(websocket: WebSocket):
        await websocket.close(code=1008)

    client.app.add_api_websocket_route("/api/guarded", refuses)
    assert "/api/guarded" in walk(client)[0]

    async def lets_in(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    client.app.add_api_websocket_route("/api/live", lets_in)
    with pytest.raises(pytest.fail.Exception, match="websocket /api/live lets anyone in"):
        walk(client)


def test_the_walk_fails_on_a_kind_of_route_it_doesnt_know(site):
    client, _, _, _ = site
    client.app.router.routes.append(Host("other.example", app=client.app))
    with pytest.raises(pytest.fail.Exception, match=r"Host\(host='other.example'.*a kind of route this walk can't ask"):
        walk(client)


def test_the_open_routes_are_only_sign_in_the_shell_and_recording_sessions():
    assert is_open("/") and is_open("/sign-in") and is_open("/sign-in/invite/abc") and is_open("/assets/index.js")
    assert is_open("/api/session/tok/begin") and is_open("/api/sign-in/options")
    assert not is_open("/api/events") and not is_open("/api/sessions") and not is_open("/api/status")
    assert not is_open("/sign-out") and not is_open("/api/me")
    assert not is_open("/sign-inside") and not is_open("/api/sign-in-as") and not is_open("/api/sign-insecure/x")


def test_an_invite_makes_a_passkey_and_signs_in_with_a_strict_httponly_cookie(site):
    client, accounts, _, _ = site
    _, made = make_passkey(client, accounts, Authenticator())
    assert made.status_code == 200 and made.json() == {"person": "stacey"}
    cookie = made.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert f"Max-Age={int(SESSION_S)}" in cookie and "Secure" not in cookie  # plain HTTP here
    assert client.get("/api/events").status_code == 200 and client.get("/api/me").json() == {"person": "stacey"}


def test_signing_in_again_with_the_passkey_and_out(site):
    client, accounts, _, log = site
    phone = Authenticator()
    make_passkey(client, accounts, phone)
    client.post("/api/sign-out")
    assert client.get("/api/events").status_code == 401
    signed = sign_in(client, phone)
    assert signed.status_code == 200 and signed.json() == {"person": "stacey"}
    assert client.get("/api/events").status_code == 200
    assert log.store.row("SELECT sign_count, last_used FROM passkeys")["sign_count"] == 2


def test_an_invite_works_once_and_for_a_day(site):
    client, accounts, clock, _ = site
    token, _ = make_passkey(client, accounts, Authenticator())
    assert client.post(f"/api/sign-in/invite/{token}/options").status_code == 410
    assert client.get(f"/sign-in/invite/{token}").status_code == 410
    old = accounts.invite("Alon")
    clock.t += INVITE_S + 1
    assert client.post(f"/api/sign-in/invite/{old}/options").status_code == 410


def test_a_passkey_answer_for_another_site_or_a_used_challenge_is_refused(site):
    client, accounts, _, _ = site
    phone = Authenticator()
    make_passkey(client, accounts, phone)
    client.cookies.clear()
    assert sign_in(client, phone, origin="http://evil.example").status_code == 401
    asked = client.post("/api/sign-in/options").json()
    answer = {"ceremony": asked["ceremony"], "credential": phone.get(asked["options"])}
    assert client.post("/api/sign-in", json=answer).status_code == 200
    client.cookies.clear()
    assert client.post("/api/sign-in", json=answer).status_code == 401  # each challenge is answered once


def test_a_passkey_tars_doesnt_know_signs_nobody_in(site):
    client, _, _, _ = site
    assert sign_in(client, Authenticator()).status_code == 401 and client.get("/api/events").status_code == 401


def test_a_session_ends_after_30_days(site):
    client, accounts, clock, _ = site
    make_passkey(client, accounts, Authenticator())
    clock.t += SESSION_S + 1
    assert client.get("/api/events").status_code == 401


def test_signing_someone_out_everywhere_forgets_their_passkeys_and_voids_their_invites(site):
    client, accounts, _, _ = site
    phone = Authenticator()
    make_passkey(client, accounts, phone, person=" Stacey ")
    pending = accounts.invite("STACEY")
    assert accounts.remove("stacey ") == (1, 1, 1)
    assert client.get("/api/events").status_code == 401 and sign_in(client, phone).status_code == 401
    assert client.post(f"/api/sign-in/invite/{pending}/options").status_code == 410
    assert accounts.remove("Stacey") == (0, 0, 0)


def test_a_name_is_the_same_person_whatever_its_case_or_spaces(site):
    client, accounts, _, _ = site
    token = accounts.invite("  mary ann ")
    assert "Welcome, Mary Ann" in client.get(f"/sign-in/invite/{token}").text
    make_passkey(client, accounts, Authenticator(), person="Mary Ann")
    assert len(accounts.passkeys("MARY ANN")) == 1


def test_a_passkey_handing_back_someone_elses_user_id_is_refused(site):
    from voice_assistant.signin import user_id

    client, accounts, _, _ = site
    phone = Authenticator()
    make_passkey(client, accounts, phone)
    client.cookies.clear()
    assert sign_in(client, phone, handle=user_id("alon")).status_code == 401
    assert sign_in(client, phone, handle=user_id("Stacey")).status_code == 200


def test_old_sessions_and_invites_are_cleared_out_as_new_ones_are_made(site):
    client, accounts, clock, log = site
    accounts.invite("Alon")
    make_passkey(client, accounts, Authenticator())
    clock.t += SESSION_S + 1
    accounts.invite("Stacey")
    assert log.store.row("SELECT count(*) AS n FROM web_sessions")["n"] == 0
    assert [r["person"] for r in log.store.rows("SELECT person FROM web_invites")] == ["stacey"]


def test_only_so_many_challenges_wait_at_once_the_oldest_going_first():
    ceremonies = Ceremonies(clock=Clock())
    first = ceremonies.start(b"first")
    ids = [ceremonies.start(bytes([i % 256])) for i in range(MOST_CEREMONIES)]
    assert ceremonies.take(first) is None and ceremonies.take(ids[-1]) is not None and ceremonies.take(ids[1])


def test_changes_from_another_site_are_still_refused_before_sign_in(site):
    client, _, _, _ = site
    r = client.post("/api/sign-in/options", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_the_invite_page_escapes_the_name(site):
    client, accounts, _, _ = site
    token = accounts.invite("<script>x</script>")
    page = client.get(f"/sign-in/invite/{token}").text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page


def test_without_sign_in_the_web_ui_is_as_before(tmp_path):
    client = TestClient(create_app(EventLog(tmp_path / "events")), base_url=ORIGIN)
    assert client.get("/api/events").status_code == 200 and client.get("/sign-in").status_code == 404


def cli_config(**web):
    import dataclasses
    from pathlib import Path

    from voice_assistant.config import TelegramConfig, WebConfig, load_config

    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    return dataclasses.replace(cfg, web=WebConfig(**web), telegram=TelegramConfig(enabled=False))


def test_an_invite_over_https_needs_web_link_to_be_a_name(tmp_path):
    from voice_assistant import wiring as cli
    from voice_assistant.config import ConfigError

    with pytest.raises(ConfigError, match=r"\[web\] link"):
        cli.web_invite(cli_config(sign_in=True, tls_cert="c.pem", tls_key="k.pem"), tmp_path, "Stacey", 8080)


def test_links_without_web_link_are_https_when_the_web_ui_is(monkeypatch):
    from voice_assistant import wiring as cli
    from voice_assistant.browser import login_page

    monkeypatch.setattr(login_page, "reachable_address", lambda: "100.64.0.1")
    assert cli.web_link(cli_config(tls_cert="c.pem", tls_key="k.pem"), 8080) == "https://100.64.0.1:8080"
    assert cli.web_link(cli_config(), 8080) == "http://100.64.0.1:8080"
    assert cli.web_link(cli_config(link="https://pi.tail.ts.net:8080/"), 8080) == "https://pi.tail.ts.net:8080"


def test_signing_out_someone_tars_doesnt_know_says_so(tmp_path, capsys):
    from voice_assistant import wiring as cli

    cfg = cli_config(link="https://pi.tail.ts.net:8080")
    cli.web_invite(cfg, tmp_path, "Stacey", 8080)
    cli.web_sign_out(cfg, tmp_path, "stacey")
    assert "1 unused invite(s) voided" in capsys.readouterr().out
    cli.web_sign_out(cfg, tmp_path, "Stacey")
    assert "Nobody called Stacey has a passkey, a session or an invite" in capsys.readouterr().out
