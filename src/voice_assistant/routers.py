"""The home network's routers, asked which devices are connected now: who's home is whose phone is (presence.py).

Each router is signed into with its admin password, the way its own web page does it, and keeps the session until the
router ends it. Two kinds so far:

- A Technicolor (Vantiva) Homeware gateway, as ISPs hand out: SRP-6 sign-in at /authenticate, then the device list at
  /modals/device-modal.lp, a page of cards or a table. It lists devices that have gone too, marked offline, and can
  keep an old hardware address under a device's name after the device moved to a private one, so a device is matched
  by its name as well as its address.
- A Xiaomi router (MiWiFi; the AX3000T, as a router or an access point): a hashed password at /api/xqsystem/login for
  a token, then /api/misystem/devicelist, which lists only the devices connected to it.

A router that doesn't answer, refuses the password or sends something unexpected raises RouterError: the presence
tracker counts it as not knowing, never as everyone gone. A Homeware gateway shuts sign-in out for a while after wrong
passwords, so a refused password isn't tried again until RETRY_REFUSED_S has passed.
"""

import hashlib
import html
import random
import re
import secrets
import time
from collections.abc import Callable
from typing import NamedTuple

import httpx

TECHNICOLOR, XIAOMI = "technicolor", "xiaomi"
KINDS = (TECHNICOLOR, XIAOMI)
HTTP_TIMEOUT_S = 8.0
# A refused password isn't tried again for this long: Homeware locks sign-in out after a few wrong ones.
RETRY_REFUSED_S = 15 * 60.0


class RouterError(Exception):
    """The router didn't answer, refused the password, or sent something this doesn't understand."""


class Refused(RouterError):
    """The router turned the password down."""


class Seen(NamedTuple):
    """A device the router says is connected now: its hardware address (lower case) and the name it goes by there."""

    mac: str
    name: str


class Router:
    """One router: `connected()` is what it sees now. Signs in when it must, and again when the session ends."""

    kind = ""

    def __init__(self, name: str, address: str, user: str, password: str, transport: httpx.BaseTransport | None = None):
        self.name, self.address, self.user = name, address, user
        self._password = password
        self._transport = transport
        self._http: httpx.Client | None = None
        self._refused_at: float | None = None

    def connected(self) -> list[Seen]:
        """The devices connected now. RouterError if it can't tell."""
        if self._refused_at is not None and time.monotonic() - self._refused_at < RETRY_REFUSED_S:
            raise Refused(f"it refused the password; trying again {RETRY_REFUSED_S / 60:.0f} minutes after that")
        try:
            if self._http is None:
                self._sign_in()
            try:
                return self._read()
            except _SignedOut:
                self._sign_in()
                return self._read()
        except Refused:
            self._refused_at = time.monotonic()
            self.close()
            raise
        except httpx.HTTPError as e:
            self.close()
            raise RouterError(f"no answer from {self.address}: {type(e).__name__}") from None
        except (ValueError, KeyError, TypeError) as e:
            self.close()
            raise RouterError(f"an answer this doesn't understand: {e}") from None

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _client(self, **kw) -> httpx.Client:
        self.close()
        self._http = httpx.Client(timeout=HTTP_TIMEOUT_S, transport=self._transport, **kw)
        return self._http

    def _sign_in(self) -> None:
        raise NotImplementedError

    def _read(self) -> list[Seen]:
        raise NotImplementedError


class _SignedOut(Exception):
    """The session ended (the router restarted, or timed it out): sign in again."""


# Technicolor Homeware: SRP-6 as its /js/srp-min.js does it, byte for byte. RFC 5054's 2048-bit group, SHA-256, its own
# fixed multiplier k, A and B padded to the group's size for u, and the salt and B hashed exactly as sent.
N = int(
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD"
    "7F48A9DA04FD50E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF7473"
    "59D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748544523B524B0D57D5EA7"
    "7A2775D2ECFA032CFBDBF52FB3786160279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB694B5C8"
    "03D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73",
    16,
)
G = 2
K = int("05b9e8ef059c6b32ea59fc1d322d37f04aa30bae5aa9003b8321e21ddb04e300", 16)
N_BYTES = 256


def _bytes(n: int) -> bytes:
    """Big-endian with no leading zeros, as the page writes A and S."""
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def _sha256(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


class Srp:
    """The client's half of a Homeware sign-in. `a` is fixed only in tests."""

    def __init__(self, user: str, password: str, a: int | None = None):
        self.user, self._password = user, password
        self.a = a if a is not None else int.from_bytes(secrets.token_bytes(32), "big")
        self.A = pow(G, self.a, N)

    def proof(self, salt: bytes, b: bytes) -> tuple[bytes, bytes]:
        """(M to send, the proof the router must answer with), from its salt and B as it sent them."""
        b_pub = int.from_bytes(b, "big")
        u = int.from_bytes(_sha256(self.A.to_bytes(N_BYTES, "big"), b_pub.to_bytes(N_BYTES, "big")), "big")
        if b_pub % N == 0 or u == 0:
            raise ValueError("the router sent an invalid B")
        x = int.from_bytes(_sha256(salt, _sha256(f"{self.user}:{self._password}".encode())), "big")
        key = _sha256(_bytes(pow(b_pub - K * pow(G, x, N), self.a + u * x, N)))
        hn_xor_hg = bytes(p ^ q for p, q in zip(_sha256(_bytes(N)), _sha256(_bytes(G)), strict=True))
        m = _sha256(hn_xor_hg, _sha256(self.user.encode()), salt, _bytes(self.A), b, key)
        return m, _sha256(_bytes(self.A), m, key)


CSRF = re.compile(r'<meta[^>]*name\s*=\s*"CSRFtoken"[^>]*content\s*=\s*"([^"]+)"')
DEVICE_PAGE = "/modals/device-modal.lp"


class Technicolor(Router):
    kind = TECHNICOLOR

    def _sign_in(self) -> None:
        http = self._client(base_url=f"http://{self.address}")
        srp = Srp(self.user, self._password)
        token = self._token(http)
        first = {"I": self.user, "A": _bytes(srp.A).hex()}
        reply = self._post(http, token, first)
        if reply.status_code == 403:  # the page's own answer to a 403: a fresh token from login.lp, once
            token = self._token(http)
            reply = self._post(http, token, first)
        challenge = self._json(reply)
        if "s" not in challenge or "B" not in challenge:
            raise Refused(f"it didn't take the user {self.user!r}") if "error" in challenge else ValueError(challenge)
        m, expected = srp.proof(bytes.fromhex(challenge["s"]), bytes.fromhex(challenge["B"]))
        answer = self._json(self._post(http, token, {"M": m.hex()}))
        if "M" not in answer:
            raise Refused("it refused the password")
        if bytes.fromhex(answer["M"]) != expected:
            raise RouterError(
                "its proof didn't match: whatever answered there isn't the router that knows the password"
            )

    def _token(self, http: httpx.Client) -> str:
        page = http.get("/login.lp")
        page.raise_for_status()
        match = CSRF.search(page.text)
        if not match:
            raise ValueError("no CSRFtoken on /login.lp: not a Homeware sign-in")
        return match[1]

    @staticmethod
    def _post(http: httpx.Client, token: str, form: dict) -> httpx.Response:
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{http.base_url}login.lp"}
        return http.post("/authenticate", data={"CSRFtoken": token, **form}, headers=headers)

    @staticmethod
    def _json(reply: httpx.Response) -> dict:
        if reply.status_code == 403:
            raise RouterError("it refused the sign-in token twice (403)")
        reply.raise_for_status()
        return reply.json()

    def _read(self) -> list[Seen]:
        page = self._http.get(DEVICE_PAGE)
        page.raise_for_status()
        if "srp_password" in page.text:  # the sign-in form in its place: the session ended
            raise _SignedOut
        return [Seen(mac, name) for mac, name, online in technicolor_devices(page.text) if online]


def technicolor_devices(page: str) -> list[tuple[str, str, bool]]:
    """(address, name, online) for every device on a Homeware device page, as cards or as a table. A Wi-Fi card is
    online when its AP line says Online; a wired one (no AP line) when it has an IP address; a table row when its
    status light is green."""
    devices = []
    for card in re.split(r'<div[^>]*class="popUp smallcard span4"', page)[1:]:
        title = re.search(r'class="device_poptitle">([^<]*)<', card)
        pairs = re.findall(r'class="hostinfoleft">([^<]*)</span>\s*<span class="hostinforight"[^>]*>([^<]*)<', card)
        lines = {label.strip().lower(): value.strip() for label, value in pairs}
        mac = next((v for label, v in lines.items() if "mac" in label), "")
        if mac:
            ip = next((v for label, v in lines.items() if "ip" in label.split()), "")  # "IP Address", any language
            online = lines["ap"] == "Online" if lines.get("ap") else bool(ip)
            devices.append((mac.lower(), html.unescape(title[1].strip()) if title else "", online))
    if devices:
        return devices
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.DOTALL)
    if not rows:
        return []
    headers = [_text(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", rows[0], re.DOTALL)]
    name_at = next((i for i, h in enumerate(headers) if h in ("hostname", "name")), None)
    mac_at = next((i for i, h in enumerate(headers) if "mac" in h), None)
    if mac_at is None:
        return []
    for row in rows[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) <= mac_at:
            continue
        light = re.search(r'class="light ([\w-]+)"', row)
        name = _text(cells[name_at]) if name_at is not None and name_at < len(cells) else ""
        devices.append((_text(cells[mac_at]).lower(), name, light is None or light[1] == "green"))
    return devices


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", fragment))).strip()


# Xiaomi (MiWiFi): the page's own Encrypt.oldPwd, sha256 since newEncryptMode 1 (sha1 before).
XIAOMI_KEY = re.compile(r"key:\s*'([0-9a-f]+)'")
XIAOMI_DEVICE_ID = re.compile(r"deviceId\s*=\s*'([0-9a-fA-F:]+)'")


def xiaomi_password(password: str, key: str, nonce: str, sha256: bool = True) -> str:
    h: Callable = hashlib.sha256 if sha256 else hashlib.sha1
    return h((nonce + h((password + key).encode()).hexdigest()).encode()).hexdigest()


class Xiaomi(Router):
    kind = XIAOMI

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._token = ""

    def _sign_in(self) -> None:
        # Its pages are HTTPS with a certificate it made itself; this only ever talks to it on the home network.
        http = self._client(base_url=f"https://{self.address}", verify=False)
        info = http.get("/cgi-bin/luci/api/xqsystem/init_info")
        info.raise_for_status()
        page = http.get("/cgi-bin/luci/web")
        page.raise_for_status()
        key, device_id = XIAOMI_KEY.search(page.text), XIAOMI_DEVICE_ID.search(page.text)
        if not key or not device_id:
            raise ValueError("no key or deviceId on its sign-in page: not a MiWiFi sign-in")
        nonce = f"0_{device_id[1]}_{int(time.time())}_{random.randrange(10000)}"
        hashed = xiaomi_password(self._password, key[1], nonce, sha256=info.json().get("newEncryptMode") == 1)
        reply = http.post("/cgi-bin/luci/api/xqsystem/login", data={"password": hashed, "logtype": 2, "nonce": nonce})
        reply.raise_for_status()
        answer = reply.json()
        if answer.get("code") != 0 or not answer.get("token"):
            raise Refused(f"it refused the password (code {answer.get('code')})")
        self._token = answer["token"]

    def _read(self) -> list[Seen]:
        reply = self._http.get(f"/cgi-bin/luci/;stok={self._token}/api/misystem/devicelist")
        if reply.status_code in (401, 403):
            raise _SignedOut
        reply.raise_for_status()
        answer = reply.json()
        if answer.get("code") != 0:
            raise _SignedOut  # an expired token answers with an error code
        return xiaomi_devices(answer)


def xiaomi_devices(answer: dict) -> list[Seen]:
    """The devices connected to it now, from /api/misystem/devicelist: it lists only those."""
    return [
        Seen(str(d["mac"]).lower(), str(d.get("name") or d.get("oname") or ""))
        for d in answer.get("list", [])
        if d.get("mac") and str(d.get("online", 1)) != "0"
    ]


MAKERS: dict[str, type[Router]] = {TECHNICOLOR: Technicolor, XIAOMI: Xiaomi}


def make(spec: dict, password: str, transport: httpx.BaseTransport | None = None) -> Router:
    """A [[presence.routers]] entry, with its password from .env."""
    return MAKERS[spec["kind"]](
        spec.get("name") or spec["address"], spec["address"], spec.get("user", "admin"), password, transport
    )
