"""TARS's logins for the sites its browser works (a prototype): each tied to the exact origins it may be used on, kept
encrypted, filled in by code and never shown to a model. Passwords go in here only from a terminal.

    uv run --all-extras python tools/browser_vault.py add PERSON NAME --origin https://example.com [--origin ...]
    uv run --all-extras python tools/browser_vault.py list
    uv run --all-extras python tools/browser_vault.py remove PERSON NAME

Each household member has their own logins (by their name in [telegram] people): a task for one of them only ever
uses theirs.

The vault is ~/.cache/tars-browser/vault.enc, encrypted (Fernet) with the key beside it in vault.key, both readable
only by this user. On one disk that guards a copy of the vault on its own (a backup, a file sent somewhere), not the
machine itself; on the Pi the key would live elsewhere (systemd's credentials)."""

import argparse
import getpass
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

FOLDER = Path.home() / ".cache/tars-browser"
VAULT, KEY = FOLDER / "vault.enc", FOLDER / "vault.key"


@dataclass(frozen=True)
class Login:
    name: str
    origins: tuple[str, ...]
    username: str
    password: str

    def __repr__(self) -> str:  # never the password, whatever prints it
        return f"Login({self.name!r}, {self.origins}, {self.username!r})"


def origin(url: str) -> str:
    """scheme://host[:port], lower case, the default port dropped: what a page's address is compared by."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def _fernet() -> Fernet:
    FOLDER.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not KEY.exists():
        KEY.write_bytes(Fernet.generate_key())
        KEY.chmod(0o600)
    return Fernet(KEY.read_bytes())


def _load() -> dict[str, dict]:
    """Every person's logins: {person: {login name: entry}}."""
    if not VAULT.exists():
        return {}
    data = json.loads(_fernet().decrypt(VAULT.read_bytes()))
    if "people" not in data:  # the first prototype's vault: one person's logins, not yet said whose
        raise SystemExit("the vault is from before it was per person: run browser_vault.py migrate PERSON")
    return data["people"]


def _save(people: dict[str, dict]) -> None:
    data = _fernet().encrypt(json.dumps({"people": people}).encode())
    tmp = VAULT.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    os.replace(tmp, VAULT)


def _logins(person: str) -> list[Login]:
    return [Login(n, tuple(e["origins"]), e["username"], e["password"]) for n, e in _load().get(person, {}).items()]


def for_page(url: str, person: str) -> Login | None:
    """`person`'s login for the page at `url`: only one whose origins include the page's exactly, and only over
    HTTPS. Nothing for a look-alike (amazon.com.evil.example, an http page, another port), and never someone else's."""
    here = origin(url)
    if not here.startswith("https://"):
        return None
    return next((login for login in _logins(person) if here in login.origins), None)


def logins_for(where: str, person: str) -> list[Login]:
    """Every login `person` has for exactly this origin, of any scheme TARS uses (https for sites, imaps for mail):
    several mailboxes share one mail server."""
    here = origin(where)
    return [login for login in _logins(person) if here in login.origins]


SCHEMES = ("https://", "imaps://")  # what a login may be for: web sites, and mail over TLS


def store(person: str, name: str, origins: list[str], username: str, password: str) -> None:
    """Adds or replaces one of `person`'s logins, for exactly these origins (https or imaps only)."""
    origins = sorted({origin(o) for o in origins})
    if not person:
        raise ValueError("a login belongs to someone")
    if not origins or not all(o.startswith(SCHEMES) and "." in o for o in origins):
        raise ValueError(f"origins must be https:// or imaps:// host addresses, got {origins}")
    if not username or not password:
        raise ValueError("both a username and a password are needed")
    people = _load()
    people.setdefault(person, {})[name] = {"origins": origins, "username": username, "password": password}
    _save(people)


def migrate(person: str) -> int:
    """The first prototype's vault, its logins made `person`'s; how many."""
    data = json.loads(_fernet().decrypt(VAULT.read_bytes()))
    if "people" in data:
        return 0
    _save({person: data})
    return len(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add", help="add or replace a login (asks for the password)")
    a.add_argument("person", help="whose: a name in [telegram] people")
    a.add_argument("name")
    a.add_argument("--origin", action="append", required=True, help="e.g. https://www.amazon.com (more than once ok)")
    sub.add_parser("list")
    r = sub.add_parser("remove")
    r.add_argument("person")
    r.add_argument("name")
    m = sub.add_parser("migrate", help="make the first prototype's vault one person's")
    m.add_argument("person")
    args = parser.parse_args()
    if args.command == "migrate":
        print(f"{migrate(args.person)} login(s) are now {args.person}'s")
        return
    people = _load()
    if args.command == "add":
        username = input("Username (email or phone): ").strip()
        password = getpass.getpass("Password (not shown): ")
        try:
            store(args.person, args.name, args.origin, username, password)
        except ValueError as e:
            raise SystemExit(f"nothing saved: {e}") from None
        print(f"Saved {args.name} for {', '.join(sorted({origin(o) for o in args.origin}))}.")
        return
    elif args.command == "list":
        for person, entries in sorted(people.items()):
            for name, e in sorted(entries.items()):
                print(f"{person}: {name}: {e['username']} on {', '.join(e['origins'])}")
        if not people:
            print("(empty)")
    else:
        print("removed" if people.get(args.person, {}).pop(args.name, None) else "no such login")
        _save(people)


if __name__ == "__main__":
    main()
