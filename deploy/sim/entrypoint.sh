#!/bin/sh
# What the device's services and its install script do, for the container: a virtual screen up (for developing the
# browser here: on the Pi it runs on a Fly Sprite), the browser's profile folder private, TARS's packages installed;
# then the command.
set -e
rm -f /tmp/tars-sim-ready  # a restarted device isn't ready until its packages are in again
if command -v Xvfb >/dev/null; then
  Xvfb :99 -screen 0 1280x1000x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
fi
mkdir -p "$HOME/.cache/tars-browser" && chmod 700 "$HOME/.cache/tars-browser"
uv sync --frozen --all-extras --quiet
touch /tmp/tars-sim-ready
exec "$@"
