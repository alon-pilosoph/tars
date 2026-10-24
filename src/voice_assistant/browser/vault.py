"""TARS's logins for the sites its browser signs in to, and for the mailboxes it reads sign-in emails from: each one
a household member's, tied to the exact sites (origins) it may be used on, kept encrypted, and used by code
(login.py, mail.py), never shown to a model.

    uv run --extra browser python -m voice_assistant.browser vault add https://www.ksp.co.il [--person NAME]
    uv run --extra browser python -m voice_assistant.browser vault list
    uv run --extra browser python -m voice_assistant.browser vault remove www.ksp.co.il [--person NAME]

A login TARS doesn't have yet is also asked for while it works (login.py), in the terminal it runs in.

The vault is ~/.cache/tars-browser/vault.enc, encrypted (Fernet) with the key beside it in vault.key, both readable
only by TARS's user, and like the browser's profile kept out of the backup. On one disk that guards a copy of the
vault on its own (a backup, a file sent somewhere), not the machine itself."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

FOLDER = Path.home() / ".cache/tars-browser"
SCHEMES = ("https://", "imaps://")  # what a login may be for: sites, and mail over TLS
GMAIL = "imaps://imap.gmail.com"


@dataclass(frozen=True)
class Login:
    name: str
    origins: tuple[str, ...]
    username: str
    password: str  # empty for a site that signs in by a code alone

    def __repr__(self) -> str:  # never the password, whatever prints it
        return f"Login({self.name!r}, {self.origins}, {self.username!r})"


def origin(url: str) -> str:
    """scheme://host[:port], lower case, the default port dropped: what a page's address is compared by."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


class Vault:
    """The logins, as {person: {login name: {origins, username, password}}}, read and written whole."""

    def __init__(self, folder: Path = FOLDER):
        self.folder = folder
        self.file, self.key = folder / "vault.enc", folder / "vault.key"

    def _fernet(self):
        from cryptography.fernet import Fernet

        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.key.exists():
            self.key.write_bytes(Fernet.generate_key())
            self.key.chmod(0o600)
        return Fernet(self.key.read_bytes())

    def _load(self) -> dict[str, dict]:
        if not self.file.exists():
            return {}
        return json.loads(self._fernet().decrypt(self.file.read_bytes()))["people"]

    def _save(self, people: dict[str, dict]) -> None:
        data = self._fernet().encrypt(json.dumps({"people": people}).encode())
        tmp = self.file.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        os.replace(tmp, self.file)

    def logins(self, person: str) -> list[Login]:
        return [Login(n, tuple(e["origins"]), e["username"], e["password"])
                for n, e in self._load().get(person, {}).items()]  # fmt: skip

    def everyone(self) -> dict[str, list[Login]]:
        return {person: self.logins(person) for person in sorted(self._load())}

    def for_page(self, url: str, person: str) -> Login | None:
        """`person`'s login for the page at `url`: only one whose origins include the page's exactly, and only over
        HTTPS. Nothing for a look-alike (ksp.co.il.evil.example, an http page, another port), never someone else's."""
        here = origin(url)
        if not here.startswith("https://"):
            return None
        return next((login for login in self.logins(person) if here in login.origins), None)

    def mailboxes(self, person: str) -> list[Login]:
        """`person`'s mailboxes TARS may read (imaps:// logins): never typed into a page, since none is https."""
        return [login for login in self.logins(person) if all(o.startswith("imaps://") for o in login.origins)]

    def store(self, person: str, origins: list[str], username: str, password: str = "", name: str = "") -> Login:
        """Adds or replaces `person`'s login for exactly these origins (https sites, or an imaps mail server), named
        `name`, or by the first one's host."""
        found = sorted({origin(o) for o in origins})
        if not person:
            raise ValueError("a login belongs to someone")
        if not found or not all(o.startswith(SCHEMES) and "." in o for o in found):
            raise ValueError(f"a login is for https:// sites or an imaps:// mail server, not {', '.join(origins)}")
        if not username:
            raise ValueError("a login needs a username (an email or a phone number)")
        if found[0].startswith("imaps://") and not password:
            raise ValueError("a mailbox needs its password (for Gmail, an app password)")
        name = name or urlsplit(found[0]).hostname or found[0]
        people = self._load()
        people.setdefault(person, {})[name] = {"origins": found, "username": username, "password": password}
        self._save(people)
        return Login(name, tuple(found), username, password)

    def store_mailbox(self, person: str, address: str, password: str, server: str = GMAIL) -> Login:
        """Adds or replaces one of `person`'s mailboxes: one login per address, as several share a server."""
        host = urlsplit(origin(server)).hostname or server
        return self.store(
            person, [server], address.strip(), password.replace(" ", ""), name=f"{host} {address.strip()}"
        )

    def remove(self, person: str, name: str) -> bool:
        people = self._load()
        if people.get(person, {}).pop(name, None) is None:
            return False
        self._save(people)
        return True


def household_person(config: Path) -> str:
    """Whose logins a task uses when nobody says: the first of [telegram] people, as TARS knows the household."""
    from ..config import load_config

    try:
        people = load_config(config).telegram.people
    except Exception:  # noqa: BLE001 - no config here (a test, a fresh clone): one person, unnamed
        return "me"
    return next(iter(people), "me")
