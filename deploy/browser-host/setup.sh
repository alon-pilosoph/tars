#!/usr/bin/env bash
# Sets up a Fly Sprite to be TARS's browser ([browser] sprite, docs/browser.md#on-a-sprite), from the machine that
# runs TARS's Telegram service. Safe to run again: it brings the Sprite and this machine's side back to this script's
# state, keeping the keys it already made and each person's browser profile on the Sprite.
#
#   deploy/browser-host/setup.sh tars-browser
#
# Needs the sprite command, signed in (curl -fsSL https://sprites.dev/install.sh | sh; sprite login).
#
# On the Sprite: Google Chrome, a virtual screen, sshd on its localhost that accepts one key for three forwards and
# nothing else, and the control service (control.py), each a Sprite service. Its network policy denies everything,
# lifted only while this script installs packages. Here, in ~/.cache/tars-browser beside the vault: the link's key,
# the Sprite's host key, and the control service's key.
set -euo pipefail

SPRITE="${1:-}"
if [[ ! "$SPRITE" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  echo "usage: $0 SPRITE_NAME   (lower case letters, digits and dashes)" >&2
  exit 2
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FOLDER="$HOME/.cache/tars-browser"
LINK_KEY="$FOLDER/sprite-link"
KNOWN_HOSTS="$FOLDER/sprite-known-hosts"
CONTROL_KEY="$FOLDER/sprite-control-key"
CLI="$(command -v sprite || echo "$HOME/.local/bin/sprite")"
[[ -x "$CLI" ]] || { echo "no sprite command: curl -fsSL https://sprites.dev/install.sh | sh; sprite login" >&2; exit 1; }

on() { "$CLI" exec --no-stdin -s "$SPRITE" -- bash -c "$1"; }
put() { "$CLI" exec --stdin -s "$SPRITE" -- bash -c "umask 077; mkdir -p \"\$(dirname '$2')\"; cat > '$2'" < "$1"; }
policy() {
  "$CLI" api -s "$SPRITE" /policy/network -- -sS -f -X POST -H 'Content-Type: application/json' -d "$1" >/dev/null 2>&1
}
DENY_ALL='{"rules":[{"domain":"*","action":"deny"}]}'

echo "== $SPRITE"
if ! "$CLI" list 2>/dev/null | grep -qx "$SPRITE"; then
  "$CLI" create --skip-console "$SPRITE"
fi
[[ "$(on 'uname -m')" == x86_64 ]] || { echo "the Sprite isn't x86-64: Google Chrome won't run there" >&2; exit 1; }

echo "== keys here, in $FOLDER"
mkdir -p "$FOLDER" && chmod 700 "$FOLDER"
[[ -f "$LINK_KEY" ]] || ssh-keygen -q -t ed25519 -N '' -C "tars browser link to $SPRITE" -f "$LINK_KEY"
[[ -f "$CONTROL_KEY" ]] || (umask 077 && openssl rand -hex 32 > "$CONTROL_KEY")
chmod 600 "$LINK_KEY" "$CONTROL_KEY"

echo "== packages (the network policy lifted until they're in)"
trap 'policy "$DENY_ALL" || echo "COULD NOT RESTORE THE DENY-ALL POLICY ON $SPRITE: set it by hand" >&2' EXIT
policy '{"rules":[]}'
on 'set -e
if ! command -v google-chrome >/dev/null || ! command -v Xvfb >/dev/null || ! [ -x /usr/sbin/sshd ]; then
  curl -fsSLo /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /tmp/chrome.deb xvfb openssh-server >/dev/null
  rm -f /tmp/chrome.deb
fi
google-chrome --version'
policy "$DENY_ALL"
trap - EXIT

echo "== sshd: one key, three forwards, no commands"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
printf 'command="/usr/bin/false",restrict,port-forwarding,permitopen="localhost:9333",permitopen="localhost:9334",permitlisten="localhost:1080" %s\n' \
  "$(cat "$LINK_KEY.pub")" > "$TMP/authorized_keys"
cat > "$TMP/sshd_config" <<'EOF'
# TARS's browser link (deploy/browser-host/setup.sh): the Sprite's localhost only, reached through `sprite proxy`.
Port 2222
ListenAddress 127.0.0.1
HostKey /home/sprite/ssh/host_ed25519
PidFile /home/sprite/ssh/sshd.pid
AuthorizedKeysFile /home/sprite/ssh/authorized_keys
AllowUsers sprite
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM no
StrictModes yes
AllowTcpForwarding yes
AllowStreamLocalForwarding no
AllowAgentForwarding no
GatewayPorts no
PermitTunnel no
X11Forwarding no
PermitTTY no
PermitUserRC no
ForceCommand /usr/bin/false
LogLevel VERBOSE
EOF
put "$TMP/authorized_keys" /home/sprite/ssh/authorized_keys
put "$TMP/sshd_config" /home/sprite/ssh/sshd_config
put "$HERE/control.py" /home/sprite/browser-host/control.py
put "$CONTROL_KEY" /home/sprite/browser-host/control-key
# The account is locked (no password), which sshd without PAM refuses even for a key; "*" is no password at all.
on 'set -e
chmod 700 ~/ssh ~/browser-host
[ -f ~/ssh/host_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -f ~/ssh/host_ed25519
sudo usermod -p "*" sprite
sudo mkdir -p /run/sshd'

echo "== services"
on 'for s in control chrome sshd xvfb; do sprite-env services delete "$s" >/dev/null 2>&1 || true; done
pkill -x Xvfb || true; rm -f /tmp/.X99-lock
sprite-env services create xvfb --cmd /usr/bin/Xvfb --args :99,-screen,0,1280x1000x24,-nolisten,tcp --no-stream
sprite-env services create sshd --cmd /usr/sbin/sshd --args -D,-e,-f,/home/sprite/ssh/sshd_config --no-stream
sprite-env services create control --needs xvfb --cmd /usr/bin/python3 \
  --args -I,/home/sprite/browser-host/control.py --no-stream
sleep 2
sprite-env services list | python3 -c "import json,sys; [print(\" \", s[\"name\"], s[\"state\"][\"status\"]) for s in json.load(sys.stdin)]"'

echo "== the Sprite's host key, recorded here"
printf '%s %s\n' "$SPRITE" "$(on 'cut -d" " -f1,2 ~/ssh/host_ed25519.pub')" > "$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS"

echo "== the network policy"
"$CLI" api -s "$SPRITE" /policy/network -- -sS 2>/dev/null
echo
echo "Done. Set [browser] sprite = \"$SPRITE\" in config.local.toml, then: uv run voice-assistant --check"
