#!/usr/bin/env bash
# Sets up a Fly Sprite to be TARS's browser ([browser] sprite, docs/browser.md#on-a-sprite), from the machine that
# runs TARS's Telegram service. Safe to run again: it brings the Sprite and this machine's side back to this script's
# state, keeping the keys it already made and each person's browser profile on the Sprite.
#
#   deploy/browser-host/setup.sh tars-browser
#
# Needs the sprite command, signed in (curl -fsSL https://sprites.dev/install.sh | sh; sprite login).
#
# On the Sprite: Google Chrome, a virtual screen (KasmVNC's: the display Chrome draws to and its web viewer, for a
# hand-over), sshd on its localhost that accepts one key for three forwards and
# nothing else, and the control service (control.py), each a Sprite service. Its network policy denies everything,
# lifted only while this script installs packages. Here, in ~/.cache/tars-browser beside the vault: the link's key,
# the Sprite's host key, and the control service's key.
#
# The Sprite's address (https://<sprite>-<org>.sprites.app) is made public, for a hand-over: the person opens the
# browser's screen from their phone, which isn't signed in to Fly. Behind it is only control.py's gate, which answers
# nothing unless a hand-over is on, and then lets in the one browser with its token (docs/browser.md#handing-over).
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
ADDRESS="$FOLDER/sprite-address"
VIEWER_PASSWORD="$FOLDER/sprite-viewer-password"
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
[[ -f "$VIEWER_PASSWORD" ]] || (umask 077 && openssl rand -hex 6 > "$VIEWER_PASSWORD")
chmod 600 "$LINK_KEY" "$CONTROL_KEY"

echo "== packages (the network policy lifted until they're in)"
trap 'policy "$DENY_ALL" || echo "COULD NOT RESTORE THE DENY-ALL POLICY ON $SPRITE: set it by hand" >&2' EXIT
policy '{"rules":[]}'
on 'set -e
KASMVNC=1.5.2
if ! command -v google-chrome >/dev/null || ! [ -x /usr/sbin/sshd ] || ! command -v xdpyinfo >/dev/null \
   || ! Xvnc -version 2>&1 | grep -q "KasmVNC $KASMVNC"; then
  . /etc/os-release
  curl -fsSLo /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  curl -fsSLo /tmp/kasmvnc.deb \
    "https://github.com/kasmtech/KasmVNC/releases/download/v$KASMVNC/kasmvncserver_ubuntu_${VERSION_CODENAME}_${KASMVNC}_amd64.deb"
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /tmp/chrome.deb /tmp/kasmvnc.deb openssh-server \
    x11-utils >/dev/null
  rm -f /tmp/chrome.deb /tmp/kasmvnc.deb
fi
google-chrome --version
Xvnc -version 2>&1 | grep -o "KasmVNC [0-9.]*" | head -1'
put "$HERE/install-viewer.sh" /home/sprite/browser-host/install-viewer.sh
on 'bash /home/sprite/browser-host/install-viewer.sh'
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
printf 'guacd-hostname: 127.0.0.1\nguacd-port: 4822\n' > "$TMP/guacamole.properties"
cat > "$TMP/user-mapping.xml" <<MAPPING
<user-mapping>
  <authorize username="tars" password="$(cat "$VIEWER_PASSWORD")">
    <connection name="TARS browser">
      <protocol>vnc</protocol>
      <param name="hostname">127.0.0.1</param>
      <param name="port">5901</param>
    </connection>
  </authorize>
</user-mapping>
MAPPING
put "$TMP/guacamole.properties" /home/sprite/guac/home/guacamole.properties
put "$TMP/user-mapping.xml" /home/sprite/guac/home/user-mapping.xml
# The account is locked (no password), which sshd without PAM refuses even for a key; "*" is no password at all.
on 'set -e
chmod 700 ~/ssh ~/browser-host
[ -f ~/ssh/host_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -f ~/ssh/host_ed25519
sudo usermod -p "*" sprite
sudo mkdir -p /run/sshd'

echo "== services"
# KasmVNC is the display, its viewer on localhost only, behind the control service's gate: no VNC port, no password
# of its own (the gate is the lock), and no UDP (it would look its public address up, which the policy denies).
on 'for s in control tomcat guacd vnc view chrome sshd screen xvfb; do sprite-env services delete "$s" >/dev/null 2>&1 || true; done
pkill -x Xvfb || true; pkill -x Xvnc || true; rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
sprite-env services create screen --cmd /usr/bin/Xvnc --args \
  :99,-geometry,1280x1000,-depth,24,-interface,127.0.0.1,-websocketPort,6080,-rfbport,0,-SecurityTypes,None,-disableBasicAuth,-sslOnly,0,-httpd,/usr/share/kasmvnc/www,-AlwaysShared,-UseIPv6,0,-publicIP,127.0.0.1 \
  --no-stream
sprite-env services create vnc --needs screen --cmd /usr/bin/x11vnc \
  --args -display,:99,-localhost,-rfbport,5901,-forever,-shared,-nopw,-quiet,-noxdamage,-xrandr,newfbsize --no-stream
sprite-env services create guacd --needs vnc --cmd /usr/local/sbin/guacd --args -f,-b,127.0.0.1,-l,4822 --no-stream
sprite-env services create tomcat --needs guacd --env GUACAMOLE_HOME=/home/sprite/guac/home \
  --cmd /home/sprite/guac/tomcat/bin/catalina.sh --args run --no-stream
sprite-env services create sshd --cmd /usr/sbin/sshd --args -D,-e,-f,/home/sprite/ssh/sshd_config --no-stream
sprite-env services create control --needs screen --http-port 8080 --cmd /usr/bin/python3 \
  --args -I,/home/sprite/browser-host/control.py --no-stream
sleep 2
sprite-env services list | python3 -c "import json,sys; [print(\" \", s[\"name\"], s[\"state\"][\"status\"]) for s in json.load(sys.stdin)]"'

echo "== the Sprite's host key, recorded here"
printf '%s %s\n' "$SPRITE" "$(on 'cut -d" " -f1,2 ~/ssh/host_ed25519.pub')" > "$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS"

echo "== its address, public, with nothing behind it but the gate"
if [[ "$("$CLI" info -s "$SPRITE" | sed -n 's/^Auth: *//p')" != public ]]; then
  echo "  not public yet: run   $CLI config update -s $SPRITE --url-auth public"
fi
"$CLI" info -s "$SPRITE" | sed -n 's/^URL: *//p' > "$ADDRESS"
chmod 600 "$ADDRESS"
cat "$ADDRESS"

echo "== the network policy"
"$CLI" api -s "$SPRITE" /policy/network -- -sS 2>/dev/null
echo
echo "Done. Set [browser] sprite = \"$SPRITE\" in config.local.toml, then: uv run voice-assistant --check"
