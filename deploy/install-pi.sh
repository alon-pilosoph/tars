#!/usr/bin/env bash
# Sets TARS up on a Raspberry Pi (Raspberry Pi OS 64-bit, Bookworm or later), from a clone at ~/voice-assistant
# (deploy/sim/run.sh tries the same setup in a container first):
#
#   git clone https://github.com/alon-pilosoph/tars.git ~/voice-assistant && ~/voice-assistant/deploy/install-pi.sh
#
# Safe to run again: it adds what's missing and restarts the services (after a `git pull`, say). Everything that needs
# root goes through deploy/tars-system: the system packages (deploy/pi-packages.txt), the udev rules (starting TARS
# when the speakerphone is plugged in; a remote's IR receiver) and letting the services run without anyone logged in.
# The first run, or one after tars-system itself changed, asks for sudo to install it; after that, nothing does.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
if [ "$REPO" != "$HOME/voice-assistant" ]; then
  echo "!! The services run TARS from ~/voice-assistant, but this clone is at $REPO. Clone it there instead."
  exit 1
fi
cd "$REPO"

echo "== The system: packages, TARS's device rules, lingering (deploy/tars-system, the only thing TARS runs as root)"
# A root-owned copy of deploy/tars-system, and a sudoers rule that lets this user run it (with no arguments) and
# nothing else, so an update that needs the system changed never waits for a password (docs/deployment.md#updates).
# Putting them in place, or a changed tars-system, takes a person: sudo asks for the password once, here.
HELPER=/usr/local/sbin/tars-system
if ! cmp -s deploy/tars-system "$HELPER" || ! sudo -n -l "$HELPER" >/dev/null 2>&1; then
  if [ -n "${TARS_SILENT_CHECK:-}" ]; then
    echo "!! deploy/tars-system changed, or isn't installed yet: run deploy/install-pi.sh yourself, once" >&2
    exit 1
  fi
  sudo install -m 755 -o root -g root deploy/tars-system "$HELPER"
  rule=$(mktemp)
  echo "$USER ALL=(root) NOPASSWD: $HELPER \"\"" > "$rule"
  sudo visudo -cf "$rule" >/dev/null
  sudo install -m 440 -o root -g root "$rule" /etc/sudoers.d/tars-system
  rm -f "$rule"
fi
sudo -n "$HELPER"  # packages from deploy/pi-packages.txt, device rules, lingering
# The browser's profile keeps the sites' logins with a fixed key (no desktop keyring): only TARS's user may read it.
mkdir -p "$HOME/.cache/tars-browser" && chmod 700 "$HOME/.cache/tars-browser"
# Chromium's own folders, made here: Telegram's sandbox lets it write to them, not make them.
mkdir -p -m 700 "$HOME/.config/chromium" "$HOME/.config/chromium-headless"

echo "== uv (the services run it from ~/.local/bin)"
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || curl -LsSf https://astral.sh/uv/install.sh | sh

echo "== Python packages (uv installs its own Python)"
# Every extra: echo cancellation, Claude, Android TV, the example house's IR blaster, the S3 backup, the browser.
"$UV" sync --frozen --all-extras

echo "== API keys (TARS uses the services there are keys for, and says what it does without the others)"
[ -e .env ] || cp .env.example .env
chmod 600 .env  # the keys are for TARS's user only
if ! "$UV" run --frozen python -c "from pathlib import Path; from voice_assistant.__main__ import keyed; \
from voice_assistant.config import load_config; keyed(load_config(Path('config.toml')), Path('.'))"; then
  echo "!! Put the keys in $REPO/.env (see .env.example), then run this again"
  exit 1
fi

echo "== Models (the double-check's recognizer, the speech detectors and the speaker model download on first use)"
"$UV" run --frozen python -c "
from pathlib import Path
from voice_assistant.config import load_config
from voice_assistant.__main__ import make_trigger, make_speaker_id
from voice_assistant.recorder import make_recorder
root = Path('.').resolve(); cfg = load_config(root / 'config.toml')
make_trigger(cfg, False, root)
make_recorder(cfg, root)
if cfg.speaker.enabled: make_speaker_id(cfg, root)
"

echo "== Audio devices (put part of the speakerphone's name in config.toml [audio] if it isn't the default)"
"$UV" run --frozen voice-assistant --list-devices

echo "== Services"
mkdir -p "$HOME/.config/systemd/user"
cp deploy/voice-assistant.service deploy/voice-assistant-web.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable voice-assistant voice-assistant-web
systemctl --user restart voice-assistant voice-assistant-web  # new code on a rerun
# The browser's virtual screen, if [browser] is on (docs/browser.md).
if "$UV" run --frozen python -c "from pathlib import Path; from voice_assistant.config import load_config; \
raise SystemExit(0 if load_config(Path('config.toml')).browser.enabled else 1)"; then
  cp deploy/voice-assistant-display.service "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable voice-assistant-display
  systemctl --user restart voice-assistant-display
elif [ -e "$HOME/.config/systemd/user/voice-assistant-display.service" ]; then
  systemctl --user disable --now voice-assistant-display || true
fi
# TARS on Telegram, if [telegram] is on in config.toml (docs/telegram.md).
if "$UV" run --frozen python -c "from pathlib import Path; from voice_assistant.config import load_config; \
raise SystemExit(0 if load_config(Path('config.toml')).telegram.enabled else 1)"; then
  cp deploy/voice-assistant-telegram.service "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable voice-assistant-telegram
  systemctl --user restart voice-assistant-telegram
elif [ -e "$HOME/.config/systemd/user/voice-assistant-telegram.service" ]; then
  systemctl --user disable --now voice-assistant-telegram || true
fi
# The backup to S3, every 15 minutes, if [backup] is on (docs/backup.md).
if "$UV" run --frozen python -c "from pathlib import Path; from voice_assistant.config import load_config; \
raise SystemExit(0 if load_config(Path('config.toml')).backup.enabled else 1)"; then
  cp deploy/voice-assistant-backup.service deploy/voice-assistant-backup.timer "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable --now voice-assistant-backup.timer
elif [ -e "$HOME/.config/systemd/user/voice-assistant-backup.timer" ]; then
  systemctl --user disable --now voice-assistant-backup.timer || true
fi
# Updates by itself, every ten minutes, if [updates] is on (docs/deployment.md#updates).
if "$UV" run --frozen python -c "from pathlib import Path; from voice_assistant.config import load_config; \
raise SystemExit(0 if load_config(Path('config.toml')).updates.enabled else 1)"; then
  cp deploy/voice-assistant-update.service deploy/voice-assistant-update.timer "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable --now voice-assistant-update.timer
elif [ -e "$HOME/.config/systemd/user/voice-assistant-update.timer" ]; then
  systemctl --user disable --now voice-assistant-update.timer || true
fi

echo "== Checking everything (it says \"TARS is ready.\" and listens a moment)"
sleep 3  # the services just restarted
# TARS_SILENT_CHECK=1 (an update installing itself, maybe at night): the voice made, not said, and nothing heard.
"$UV" run --frozen voice-assistant --check ${TARS_SILENT_CHECK:+--silent} || echo "!! Fix what's marked ✗ above, then run this again."

echo
echo "Done. Say \"hey TARS\". The web UI is at http://$(hostname).local:8080"
echo "Logs: journalctl --user-unit voice-assistant -f   (and --user-unit voice-assistant-web,"
echo "      --user-unit voice-assistant-telegram, --user-unit voice-assistant-backup)"
