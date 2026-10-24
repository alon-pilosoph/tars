"""TARS's logins for the sites its browser signs in to, and for the mailboxes it reads sign-in emails from: each one
a household member's, tied to the exact sites (origins) it may be used on, kept encrypted, and used by code
(login.py, mail.py), never shown to a model.

    uv run python -m voice_assistant.browser vault add https://www.ksp.co.il [--person NAME]
    uv run python -m voice_assistant.browser vault list
    uv run python -m voice_assistant.browser vault remove www.ksp.co.il [--person NAME]

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
    """What a site's sign-in fields take, by field (login.py's keys): "username" and "password" for most sites, and
    whatever else one asks (an ID number, a customer number), under the field's own key."""

    name: str
    origins: tuple[str, ...]
    values: tuple[tuple[str, str], ...]

    def value(self, key: str) -> str | None:
        return dict(self.values).get(key)

    def values_dict(self) -> dict[str, str]:
        return dict(self.values)

    @property
    def username(self) -> str:
        return self.value("username") or ""

    @property
    def password(self) -> str:
        return self.value("password") or ""

    def __repr__(self) -> str:  # never a password or the rest, whatever prints it
        return f"Login({self.name!r}, {self.origins}, {self.username!r})"


def origin(url: str) -> str:
    """scheme://host[:port], lower case, the default port dropped: what a page's address is compared by."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


# Second-level labels under a country's: wolt.com, but ksp.co.il, ikea.co.il and bbc.co.uk.
UNDER_COUNTRY = {"co", "com", "org", "net", "gov", "ac", "edu", "muni", "ltd", "plc"}


def site(url: str) -> str:
    """The site an address is on, as its notes are kept and its email comes from: wolt.com for
    https://wolt.com/en/..., ksp.co.il for https://www.ksp.co.il/web/."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in UNDER_COUNTRY:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


class Vault:
    """The logins, as {person: {login name: {origins, username, password, fields: {key: value}}}}, read and written
    whole."""

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
        return [_login(n, e) for n, e in self._load().get(person, {}).items()]

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

    def store(self, person: str, origins: list[str], values: dict[str, str], name: str = "") -> Login:
        """Adds or replaces `person`'s login for exactly these origins (https sites, or an imaps mail server), named
        `name`, or by the first one's host, with the values its fields take (empty ones left out)."""
        found = sorted({origin(o) for o in origins})
        values = {k: v for k, v in values.items() if k and v}
        if not person:
            raise ValueError("a login belongs to someone")
        if not found or not all(o.startswith(SCHEMES) and "." in o for o in found):
            raise ValueError(f"a login is for https:// sites or an imaps:// mail server, not {', '.join(origins)}")
        if not values:
            raise ValueError("a login needs something to sign in with")
        if found[0].startswith("imaps://") and not (values.get("username") and values.get("password")):
            raise ValueError("a mailbox needs its address and its password (for Gmail, an app password)")
        name = name or urlsplit(found[0]).hostname or found[0]
        people = self._load()
        people.setdefault(person, {})[name] = _entry(found, values)
        self._save(people)
        return _login(name, people[person][name])

    def add(self, person: str, name: str, values: dict[str, str]) -> Login:
        """`person`'s login `name`, with more fields' values: what a later page of the sign-in asked for."""
        people = self._load()
        entry = people[person][name]
        both = {**_login(name, entry).values_dict(), **{k: v for k, v in values.items() if k and v}}
        people[person][name] = _entry(entry["origins"], both)
        self._save(people)
        return _login(name, people[person][name])

    def store_mailbox(self, person: str, address: str, password: str, server: str = GMAIL) -> Login:
        """Adds or replaces one of `person`'s mailboxes: one login per address, as several share a server."""
        host = urlsplit(origin(server)).hostname or server
        values = {"username": address.strip(), "password": password.replace(" ", "")}
        return self.store(person, [server], values, name=f"{host} {address.strip()}")

    def remove(self, person: str, name: str) -> bool:
        people = self._load()
        if people.get(person, {}).pop(name, None) is None:
            return False
        self._save(people)
        return True


def _entry(origins: list[str], values: dict[str, str]) -> dict:
    """A login as the vault keeps it: username and password where the prototype's vault had them, the rest apart."""
    rest = {k: v for k, v in values.items() if k not in ("username", "password")}
    return {"origins": list(origins), "username": values.get("username", ""), "password": values.get("password", ""),
            "fields": rest}  # fmt: skip


def _login(name: str, entry: dict) -> Login:
    values = {"username": entry.get("username", ""), "password": entry.get("password", ""), **entry.get("fields", {})}
    return Login(name, tuple(entry["origins"]), tuple((k, v) for k, v in values.items() if v))


def household_person(config: Path) -> str:
    """Whose logins a task uses when nobody says: the first of [telegram] people, as TARS knows the household."""
    from ..config import ConfigError, load_config

    try:
        people = load_config(config).telegram.people
    except ConfigError:  # a config.toml that can't be read: one person, unnamed, rather than no browser at all
        return "me"
    return next(iter(people), "me")
