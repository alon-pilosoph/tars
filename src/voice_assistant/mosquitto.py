"""TARS's own user on the household's Mosquitto, the one deploy/home-assistant runs, for the doors' locks
(docs/presence.md#the-front-door). deploy/install-pi.sh runs it (`python -m voice_assistant.mosquitto`); it does
something only with [[presence.doors]], the broker on this machine, and Mosquitto set up here (docs/home.md).

- TARS's password: [presence.mqtt] secret's, in .env; a random one is made and put there if there isn't one yet.
- TARS's line in Mosquitto's passwords file, as mosquitto_passwd writes it ($7$, PBKDF2-SHA512), added, or replaced
  when it isn't for the password in .env. The file is Mosquitto's own (user 1883, mode 600, as it requires), so it's
  read and written through Mosquitto's image, and replaced whole, never left half-written.
- The broker given what changed: `docker compose up -d mosquitto` (it's recreated only when compose.yaml changed, a
  new mount say), then a SIGHUP, which has Mosquitto read its passwords and its ACL (mosquitto/acl) again.

Nothing else: Home Assistant's and each device's users are a person's to make, with passwords they type in.
"""

import base64
import hashlib
import hmac
import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values

from .config import Config

HOUSE = Path("deploy") / "home-assistant"  # compose.yaml, and mosquitto/ beside it
IMAGE = "eclipse-mosquitto:2"  # compose.yaml's
CONTAINER = "mosquitto"  # compose.yaml's container_name
# Mosquitto 2.0's mosquitto_passwd defaults (2.1's are 1000 and 64): both versions read these. Few rounds are
# enough for TARS's password, a random one of 192 bits, which no amount of guessing finds.
ITERATIONS = 101
SALT_BYTES = 12
NEW = ".passwords.new"  # written beside the passwords, then moved over them
TIMEOUT_S = 120.0  # each docker command; the image may need pulling the first time


def password_line(user: str, password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    """A line of Mosquitto's passwords file, as `mosquitto_passwd` writes it: user:$7$iterations$salt$hash."""
    salt = salt if salt is not None else os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations)
    return f"{user}:$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def is_for(line: str, password: str) -> bool:
    """Whether a passwords-file line (a $7$ one) is for `password`. Any other kind is taken as not."""
    try:
        user, rest = line.strip().split(":", 1)
        _, kind, iterations, salt, digest = rest.split("$")
        if kind != "7":
            return False
        made = password_line(user, password, base64.b64decode(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(made.split("$")[-1], digest)


def _local(address: str) -> bool:
    host = socket.gethostname().lower()
    return address.strip().lower() in ("localhost", "127.0.0.1", "::1", host, f"{host}.local")


def set_up(
    cfg: Config,
    root: Path,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    make_password: Callable[[], str] = lambda: secrets.token_urlsafe(24),
) -> str:
    """Gives TARS its user on the Mosquitto here, as the module says: what it did, or why not, in a line."""
    p = cfg.presence
    if not p.doors:
        return "no [[presence.doors]]: TARS doesn't use the broker"
    mqtt = p.mqtt
    user, secret, address = mqtt.get("user"), mqtt.get("secret"), str(mqtt.get("address") or "")
    if not (user and secret):
        return "!! [presence.mqtt] needs user and secret: TARS's own user on the broker (docs/presence.md)"
    if not _local(address):
        return (
            f"the broker at {address} isn't this machine's: give TARS its user {user} there yourself, with "
            f"{secret}'s password from .env, and keep the locks' topics to the locks (docs/presence.md#the-front-door)"
        )
    folder = (root / HOUSE / "mosquitto").resolve()
    if not (folder / "passwords").exists():
        return (
            "Mosquitto isn't set up on this machine yet (docs/home.md, the house: its passwords): once it is, run "
            "deploy/install-pi.sh again and TARS adds its own user"
        )

    env = root / ".env"
    password = dotenv_values(env).get(secret) if env.exists() else None
    made = not password
    if made:
        password = make_password()
        with env.open("a") as f:
            f.write("\n# TARS's password on the MQTT broker ([presence.mqtt]), made by deploy/install-pi.sh\n")
            f.write(f"{secret}={password}\n")
        env.chmod(0o600)

    def docker(*args: str, stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return run(
            ["docker", *args], input=stdin, capture_output=True, text=True, cwd=cwd, timeout=TIMEOUT_S, check=False
        )

    try:
        running = docker("inspect", "-f", "{{.State.Running}}", CONTAINER).stdout.strip() == "true"
        if running:  # first, so the folder is mounted the way it is now before its files change
            up = docker("compose", "--profile", "mqtt", "up", "-d", CONTAINER, cwd=root / HOUSE)
            if up.returncode:
                return f"!! couldn't bring Mosquitto up to date: {(up.stderr or up.stdout).strip()}"
        mount = ["run", "--rm", "-i", "-v", f"{folder}:/m", IMAGE]
        read = docker(*mount, "cat", "/m/passwords")
        if read.returncode:
            return f"!! couldn't read Mosquitto's passwords: {(read.stderr or read.stdout).strip()}"
        lines = read.stdout.splitlines()
        mine = [line for line in lines if line.split(":", 1)[0] == user]
        changed = not (len(mine) == 1 and is_for(mine[0], password))
        if changed:
            kept = [line for line in lines if line.strip() and line.split(":", 1)[0] != user]
            new = "\n".join([*kept, password_line(user, password)]) + "\n"
            script = f"cat > /m/{NEW} && chown 1883:1883 /m/{NEW} && chmod 600 /m/{NEW} && mv -f /m/{NEW} /m/passwords"
            wrote = docker(*mount, "sh", "-c", script, stdin=new)
            if wrote.returncode:
                return f"!! couldn't write Mosquitto's passwords: {(wrote.stderr or wrote.stdout).strip()}"
        if running:
            docker("kill", "-s", "HUP", CONTAINER)  # its passwords and ACL, read again
    except FileNotFoundError:
        return "!! no Docker here, which runs Mosquitto (docs/home.md)"
    except subprocess.TimeoutExpired as e:
        return f"!! Docker didn't answer in time: {' '.join(e.cmd)}"
    if made:
        what = f"TARS's user {user} is on Mosquitto, with a new password in .env as {secret}"
    elif changed:
        what = f"TARS's user {user} is on Mosquitto now, with {secret}'s password from .env"
    else:
        what = f"TARS's user {user} is on Mosquitto already"
    if not running:
        what += "; Mosquitto isn't running: cd deploy/home-assistant && docker compose --profile mqtt up -d"
    return what


def main() -> None:
    from .config import load_config

    root = Path.cwd()
    said = set_up(load_config(root / "config.toml"), root)
    print(said)
    sys.exit(1 if said.startswith("!!") else 0)


if __name__ == "__main__":
    main()
