"""TARS's logins for the sites its browser works (a prototype): each tied to the exact origins it may be used on, kept
encrypted, filled in by code and never shown to a model. Passwords go in here only from a terminal.

    uv run --all-extras python tools/browser_vault.py add NAME --origin https://www.amazon.com [--origin ...]
    uv run --all-extras python tools/browser_vault.py list
    uv run --all-extras python tools/browser_vault.py remove NAME

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


def _load() -> dict:
    if not VAULT.exists():
        return {}
    return json.loads(_fernet().decrypt(VAULT.read_bytes()))


def _save(entries: dict) -> None:
    data = _fernet().encrypt(json.dumps(entries).encode())
    tmp = VAULT.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    os.replace(tmp, VAULT)


def for_page(url: str) -> Login | None:
    """The login stored for the page at `url`: only one whose origins include the page's exactly, and only over HTTPS.
    Nothing for a look-alike (amazon.com.evil.example, an http page, another port)."""
    here = origin(url)
    if not here.startswith("https://"):
        return None
    for name, e in _load().items():
        if here in e["origins"]:
            return Login(name, tuple(e["origins"]), e["username"], e["password"])
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add", help="add or replace a login (asks for the password)")
    a.add_argument("name")
    a.add_argument("--origin", action="append", required=True, help="e.g. https://www.amazon.com (more than once ok)")
    sub.add_parser("list")
    r = sub.add_parser("remove")
    r.add_argument("name")
    args = parser.parse_args()
    entries = _load()
    if args.command == "add":
        origins = sorted({origin(o) for o in args.origin})
        if not all(o.startswith("https://") and "." in o for o in origins):
            raise SystemExit(f"origins must be https://host addresses, got {origins}")
        username = input("Username (email or phone): ").strip()
        password = getpass.getpass("Password (not shown): ")
        if not username or not password:
            raise SystemExit("nothing saved: both are needed")
        entries[args.name] = {"origins": origins, "username": username, "password": password}
        _save(entries)
        print(f"Saved {args.name} for {', '.join(origins)}.")
    elif args.command == "list":
        for name, e in sorted(entries.items()):
            print(f"{name}: {e['username']} on {', '.join(e['origins'])}")
        if not entries:
            print("(empty)")
    else:
        print("removed" if entries.pop(args.name, None) else "no such login")
        _save(entries)


if __name__ == "__main__":
    main()
