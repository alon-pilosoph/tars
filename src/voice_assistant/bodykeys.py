"""Network bodies' device keys (docs/bodies.md#pairing): which machines may be a body for TARS, in which room.

`voice-assistant --body-add Kitchen` pairs a new device: it prints its key once, a line to give the body
(`tars-body-1:kitchen-3f9a1c:...`). The key is an Ed25519 private key's 32 bytes; TARS keeps only its public half,
so even someone with the database can't pose as a body. A body proves it has the key by signing the challenge its
connection starts with (bodylink.BodyListener), so the key never crosses the network either. `--body-remove Kitchen`
revokes the room's devices: the next time one connects, it's refused like any wrong key. `--body-list` says which
are paired and when each last connected.
"""

import base64
import re
import secrets
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY_PREFIX = "tars-body-1"  # a key's first part, and its version


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_text(device: str, seed: bytes) -> str:
    """The line a body is given: its device id and its private key."""
    return f"{KEY_PREFIX}:{device}:{_b64(seed)}"


def parse_key(text: str) -> tuple[str, bytes]:
    """(device id, private key) from a body's key line. Raises ValueError for one that isn't."""
    prefix, _, rest = text.strip().partition(":")
    device, _, encoded = rest.partition(":")
    if prefix != KEY_PREFIX or not device or not encoded:
        raise ValueError(f"not a body's key (it starts with {KEY_PREFIX}:)")
    try:
        seed = _unb64(encoded)
    except ValueError:
        raise ValueError("a body's key, damaged") from None
    if len(seed) != 32:
        raise ValueError("a body's key, damaged")
    return device, seed


def public_of(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()


def sign(seed: bytes, nonce: str) -> str:
    """A body's proof for a challenge: the nonce signed with its key, in hex."""
    return Ed25519PrivateKey.from_private_bytes(seed).sign(nonce.encode()).hex()


def verify(public: bytes, nonce: str, proof: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(bytes.fromhex(proof), nonce.encode())
        return True
    except InvalidSignature, ValueError:
        return False


def room_key(room: str) -> str:
    """A room's name as it's matched: "Ari's room" is "ari's room"."""
    return " ".join(room.split()).lower()


def _slug(room: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", room_key(room))).strip("-")[:24] or "body"


class BodyKeys:
    """The `bodies` table: each paired device, its room, its public key, and when it last connected."""

    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store, self._clock = store, clock

    def add(self, room: str, kind: str = "") -> str:
        """A new device for `room`: its key line, to give the body. Shown once: only its public half is kept."""
        if not room.strip():
            raise ValueError("a body needs a room's name")
        if self.listed(room):
            raise ValueError(f"{room} already has a body: --body-remove it first")
        seed = secrets.token_bytes(32)
        device = f"{_slug(room)}-{secrets.token_hex(3)}"
        self.store.write(
            "INSERT INTO bodies (device, room, kind, public_key, created) VALUES (?, ?, ?, ?, ?)",
            (device, " ".join(room.split()), kind, public_of(seed).hex(), self._clock()),
        )
        return key_text(device, seed)

    def public_key(self, device: str) -> bytes | None:
        """A device's public key, if it's paired and not revoked."""
        rows = self.store.rows("SELECT public_key FROM bodies WHERE device=? AND revoked IS NULL", (device,))
        return bytes.fromhex(rows[0]["public_key"]) if rows else None

    def room_of(self, device: str) -> str | None:
        rows = self.store.rows("SELECT room FROM bodies WHERE device=? AND revoked IS NULL", (device,))
        return rows[0]["room"] if rows else None

    def seen(self, device: str) -> None:
        self.store.write("UPDATE bodies SET last_seen=? WHERE device=?", (self._clock(), device))

    def listed(self, room: str | None = None) -> list[dict]:
        """The paired devices (not revoked), oldest first; only `room`'s, if it's given."""
        rows = self.store.rows(
            "SELECT device, room, kind, created, last_seen FROM bodies WHERE revoked IS NULL ORDER BY created"
        )
        return [r for r in rows if room is None or room_key(r["room"]) == room_key(room)]

    def remove(self, room: str) -> int:
        """Revokes `room`'s devices: how many."""
        gone = self.listed(room)
        for row in gone:
            self.store.write("UPDATE bodies SET revoked=? WHERE device=?", (self._clock(), row["device"]))
        return len(gone)
