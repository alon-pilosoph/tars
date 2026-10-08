#!/usr/bin/env bash
# Sets TARS up on a Raspberry Pi (Raspberry Pi OS 64-bit, Bookworm or later), from a clone at ~/voice-assistant:
#
#   git clone https://github.com/alon-pilosoph/tars.git ~/voice-assistant && ~/voice-assistant/deploy/install-pi.sh
#
# Safe to run again: it adds what's missing and restarts the services (after a `git pull`, say). It asks for sudo
# for two things: PortAudio (audio I/O) and letting the services start at boot without anyone logged in.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
if [ "$REPO" != "$HOME/voice-assistant" ]; then
  echo "!! The services run TARS from ~/voice-assistant, but this clone is at $REPO. Clone it there instead."
  exit 1
fi
cd "$REPO"

echo "== PortAudio"
# PortAudio for the mic and speaker; libatomic for Vosk, the wake double-check.
missing=$(for pkg in libportaudio2 libatomic1; do dpkg -s "$pkg" >/dev/null 2>&1 || echo "$pkg"; done)
if [ -n "$missing" ]; then
  sudo apt-get update
  sudo apt-get install -y $missing
fi

echo "== uv (the services run it from ~/.local/bin)"
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || curl -LsSf https://astral.sh/uv/install.sh | sh

echo "== Python packages (uv installs its own Python)"
"$UV" sync --frozen --all-extras  # echo cancellation and Claude are extras

echo "== API keys (the ones config.toml's choices need)"
KEYS=$("$UV" run --frozen python -c "from pathlib import Path; from voice_assistant.config import load_config, \
required_keys; print(' '.join(required_keys(load_config(Path('config.toml')))))")
[ -e .env ] || cp .env.example .env
unset_keys=$(for key in $KEYS; do grep -q "^$key=." .env || echo "$key"; done)
if [ -n "$unset_keys" ]; then
  echo "!! Put these in $REPO/.env, then run this again:" $unset_keys
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
systemctl --user restart voice-assistant voice-assistant-web  # picks up new code on a rerun
loginctl show-user "$USER" -p Linger | grep -q yes || sudo loginctl enable-linger "$USER"

echo "== Checking everything (it says \"TARS is ready.\" and listens a moment)"
sleep 3  # the services just restarted
"$UV" run --frozen voice-assistant --check || echo "!! Fix what's marked ✗ above, then run this again."

echo
echo "Done. Say \"hey TARS\". The web UI is at http://$(hostname).local:8080"
echo "Logs: journalctl --user -u voice-assistant -f   (and -u voice-assistant-web)"
