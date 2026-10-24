#!/usr/bin/env bash
# TARS on a simulated device, in a container: the device's OS and architecture, the system packages TARS installs on
# it, its cores and memory (as far as the Docker engine here allows). For trying a setup before taking it to the
# device. Not its speed (the host's cores are its own), its audio hardware or its services (systemd).
#
#   deploy/sim/run.sh [--device pi4] [start]          # build and start it (default: start the one there is)
#   deploy/sim/run.sh --device pi4 --speed [start]    # the same, capped to about a Pi 4's speed (until started without)
#   deploy/sim/run.sh [--device pi4] shell            # a shell on it
#   deploy/sim/run.sh [--device pi4] stop | remove    # stop it, or remove it and its volume (profile, environment)
#   deploy/sim/run.sh [--device pi4] COMMAND ...      # run anything on it, in the clone: uv run python -m ...
#
# Devices are the profiles in deploy/sim/devices/. Any Docker engine does; on a host of another architecture the
# device runs emulated (much slower). The clone is this one, mounted, with its .env; the device's browser profile is
# its own, empty until you sign in on it. SIM_CPUS and SIM_MEMORY_MB set other sizes than the profile's.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
DEVICE=pi4  # the household's Pi (docs/deployment.md)
SPEED=
while [ $# -gt 0 ]; do
  case "$1" in
    --device) DEVICE=$2; shift 2 ;;
    --speed) SPEED=1; shift ;;  # capped to about the device's speed (SPEED_CPUS in its profile)
    *) break ;;
  esac
done
PROFILE="$HERE/devices/$DEVICE.env"
[ -f "$PROFILE" ] || { echo "No device $DEVICE: one of $(ls "$HERE/devices" | sed 's/\.env$//' | xargs)"; exit 1; }
# shellcheck source=/dev/null
. "$PROFILE"
NAME=tars-sim-$DEVICE
HOME_IN=/home/$USER_NAME

start() {
  # The device's sizes, or as much as the engine has (on a Mac, its Linux VM: the engine's settings size it), or
  # with --speed about the device's speed.
  read -r have_cpus have_mem < <(docker info --format '{{.NCPU}} {{.MemTotal}}')
  cpus=${SIM_CPUS:-$(( have_cpus < CPUS ? have_cpus : CPUS ))}
  [ -z "$SPEED" ] || cpus=$SPEED_CPUS
  mem=$(( have_mem / 1048576 - 512 ))
  mem=${SIM_MEMORY_MB:-$(( mem < MEMORY_MB ? mem : MEMORY_MB ))}
  if ! docker container inspect "$NAME" >/dev/null 2>&1; then
    docker build --platform "$PLATFORM" --build-arg BASE="$BASE" --build-arg PACKAGES="$PACKAGES" \
      --build-arg USER_NAME="$USER_NAME" -t "$NAME" -f "$HERE/Dockerfile" "$REPO/deploy"
    # Chromium's sandbox needs calls Docker's default seccomp profile refuses (on the device it has them anyway), and
    # --init reaps the processes the browser leaves, as the device's init does.
    docker run -d --init --name "$NAME" --platform "$PLATFORM" --cpus "$cpus" --memory "${mem}m" --shm-size 1g \
      --security-opt seccomp=unconfined \
      -v "$REPO:$HOME_IN/voice-assistant" -v "$NAME:$HOME_IN/.cache" "$NAME" >/dev/null
  else
    [ "$(docker container inspect -f '{{.State.Running}}' "$NAME")" = true ] || docker start "$NAME" >/dev/null
    docker update --cpus "$cpus" "$NAME" >/dev/null
  fi
  if [ -n "$SPEED" ]; then
    echo "(Capped to $cpus of this machine's CPUs: about a $DEVICE's speed.)" >&2
  elif [ "$cpus" -lt "$CPUS" ] || [ "$mem" -lt "$MEMORY_MB" ]; then
    echo "(The engine has less than a $DEVICE: $cpus CPUs and $mem MB, for $CPUS and $MEMORY_MB.)" >&2
  fi
  # Ready once TARS's packages are in: the first start installs them, as the install script would.
  for _ in $(seq 120); do
    docker exec "$NAME" test -e /tmp/tars-sim-ready 2>/dev/null && return
    [ "$(docker container inspect -f '{{.State.Running}}' "$NAME")" = true ] || { docker logs "$NAME" | tail; exit 1; }
    sleep 5
  done
  echo "$NAME isn't ready after 10 minutes: docker logs $NAME"
  exit 1
}

case "${1:-start}" in
  start) start; echo "$DESCRIPTION: running as $NAME." ;;
  stop) docker stop "$NAME" >/dev/null ;;
  remove) docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$NAME" >/dev/null 2>&1 || true ;;
  shell) start; exec docker exec -it "$NAME" bash ;;
  *) start; exec docker exec -i "$NAME" "$@" ;;
esac
