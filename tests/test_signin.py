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
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from voice_assistant.events import EventLog
from voice_assistant.signin import COOKIE, INVITE_S, SESSION_S, Accounts, is_open
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

    def get(self, options: dict, origin: str = ORIGIN) -> dict:
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
                "userHandle": None,
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


def sign_in(client, phone: Authenticator, origin: str = ORIGIN):
    asked = client.post("/api/sign-in/options").json()
    return client.post(
        "/api/sign-in", json={"ceremony": asked["ceremony"], "credential": phone.get(asked["options"], origin)}
    )


def test_without_a_session_every_route_but_the_open_ones_is_refused(site):
    client, _, _, _ = site
    shut = []
    for route in client.app.routes:
        if isinstance(route, Mount):
            path, methods = f"{route.path}/x.js", {"GET"}
        elif isinstance(route, APIRoute):
            path = route.path_format.replace("{", "").replace("}", "")
            path = "/".join("1" if part in {"event_id", "turn_id", "item_id", "cluster_id", "reminder_id",
                                             "skill_id", "preference_id", "conversation_id", "index", "take_id"}
                            else part for part in path.split("/"))  # fmt: skip
            methods = route.methods
        else:
            continue
        for method in methods - {"HEAD"}:
            r = client.request(method, path)
            if is_open(route.path if isinstance(route, APIRoute) else path):
                assert r.status_code != 401 or path.startswith("/api/sign-in"), (method, path)
            else:
                assert r.status_code == 401, (method, path, r.status_code)
                shut.append(path)
    assert {"/api/events", "/api/conversations/1", "/api/audio/1/which", "/api/status", "/api/me"} <= set(shut)


def test_the_open_routes_are_only_sign_in_the_shell_and_recording_sessions():
    assert is_open("/") and is_open("/sign-in") and is_open("/sign-in/invite/abc") and is_open("/assets/index.js")
    assert is_open("/api/session/tok/begin") and is_open("/api/sign-in/options")
    assert not is_open("/api/events") and not is_open("/api/sessions") and not is_open("/api/status")
    assert not is_open("/sign-out") and not is_open("/api/me")


def test_an_invite_makes_a_passkey_and_signs_in_with_a_strict_httponly_cookie(site):
    client, accounts, _, _ = site
    _, made = make_passkey(client, accounts, Authenticator())
    assert made.status_code == 200 and made.json() == {"person": "Stacey"}
    cookie = made.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert f"Max-Age={int(SESSION_S)}" in cookie and "Secure" not in cookie  # plain HTTP here
    assert client.get("/api/events").status_code == 200 and client.get("/api/me").json() == {"person": "Stacey"}


def test_signing_in_again_with_the_passkey_and_out(site):
    client, accounts, _, log = site
    phone = Authenticator()
    make_passkey(client, accounts, phone)
    client.post("/api/sign-out")
    assert client.get("/api/events").status_code == 401
    signed = sign_in(client, phone)
    assert signed.status_code == 200 and signed.json() == {"person": "Stacey"}
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


def test_signing_someone_out_everywhere_forgets_their_passkeys(site):
    client, accounts, _, _ = site
    phone = Authenticator()
    make_passkey(client, accounts, phone)
    assert accounts.remove("stacey") == 1
    assert client.get("/api/events").status_code == 401 and sign_in(client, phone).status_code == 401


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
