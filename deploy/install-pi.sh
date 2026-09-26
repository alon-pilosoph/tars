#!/usr/bin/env bash
# Sets TARS up on a Raspberry Pi (Raspberry Pi OS 64-bit, Bookworm or later), from a clone at ~/voice-assistant:
#
#   git clone <repo> ~/voice-assistant && ~/voice-assistant/deploy/install-pi.sh
#
# Safe to run again: it only adds what's missing. It asks for sudo for two things: PortAudio (audio I/O) and
# letting the services start at boot without anyone logged in.
set -euo pipefail

REPO="$HOME/voice-assistant"
cd "$REPO"

echo "== PortAudio"
dpkg -s libportaudio2 >/dev/null 2>&1 || sudo apt-get install -y libportaudio2

echo "== uv (the services run it from ~/.local/bin)"
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || curl -LsSf https://astral.sh/uv/install.sh | sh

echo "== Python packages (uv installs its own Python)"
"$UV" sync --frozen

# Deepgram does the speech to text unless config.toml says [stt] provider = "openai".
NEEDS_DEEPGRAM=$("$UV" run --frozen python -c "
from pathlib import Path
from voice_assistant.config import load_config
print(load_config(Path('config.toml')).stt.provider == 'deepgram')
")
if [ ! -s .env ] || ! grep -q '^OPENAI_API_KEY=.' .env \
    || { [ "$NEEDS_DEEPGRAM" = True ] && ! grep -q '^DEEPGRAM_API_KEY=.' .env; }; then
  [ -e .env ] || cp .env.example .env
  echo "!! Put your OpenAI API key (and DEEPGRAM_API_KEY, for [stt] provider = \"deepgram\") in $REPO/.env,"
  echo "   then run this again."
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
systemctl --user enable --now voice-assistant voice-assistant-web
loginctl show-user "$USER" -p Linger | grep -q yes || sudo loginctl enable-linger "$USER"

echo
echo "Done. Say \"hey TARS\". The web UI is at http://$(hostname).local:8080"
echo "Logs: journalctl --user -u voice-assistant -f   (and -u voice-assistant-web)"
