# Trying TARS on a simulated device

[`deploy/sim/run.sh`](../deploy/sim/run.sh) runs TARS on a simulated device, in a container, from this clone: so a
change can be tried where TARS is deployed before it goes there. The device is a profile: its OS and architecture
(Raspberry Pi OS's Debian bookworm on arm64 for the household's Pi 4), the system packages TARS installs on it (the
same list as [`install-pi.sh`](../deploy/install-pi.sh), [`deploy/pi-packages.txt`](../deploy/pi-packages.txt)), a
normal user, its cores and memory. Any Docker engine runs it; on a Mac with Apple Silicon the arm64 device runs
natively, on an x86 machine emulated (much slower).

```bash
deploy/sim/run.sh                       # build and start a Pi 4 (the household's); --device pi5 for a Pi 5
deploy/sim/run.sh test                  # TARS's tests, on the device (any pytest arguments after it)
deploy/sim/run.sh shell                 # a shell on it, in the clone
deploy/sim/run.sh uv run python -m voice_assistant.browser view https://wolt.com   # anything, run on it
deploy/sim/run.sh --speed test          # capped to about the device's speed: a rough idea of it
deploy/sim/run.sh devices               # the profiles there are
deploy/sim/run.sh stop                  # stop it; remove: it, its environment and its browser profile
```

The clone is mounted as it is, `.env` and `config.local.toml` included, so what runs there is what's in the working
tree now, uncommitted changes and all. The device keeps its own Python environment and browser profile in a volume,
so the host's `.venv` is left alone and the device starts with nothing signed in. Each start rebuilds the image
(cached, so quick when nothing changed) and makes the device again if the image did change, keeping its volume.

## What it is and isn't

| The device's | Simulated |
|---|---|
| OS and architecture | ✅ Debian bookworm on arm64, as Raspberry Pi OS Lite is |
| System packages | ✅ the list install-pi.sh installs, so the two can't drift apart |
| A normal user, the clone at `~/voice-assistant` | ✅ as install-pi.sh has it |
| Cores and memory | ✅ the profile's, as far as the Docker engine has them (on a Mac, its Linux VM's settings) |
| Speed | 🟡 `--speed` caps the CPU to about the device's, roughly |
| TARS's browser | ✅ Debian's Chromium on a virtual screen (Xvfb), as on the Pi |
| systemd and its services | ❌ the container's entry script starts what the services would (the virtual screen) |
| Audio hardware, the GPU | ❌ |
| Raspberry Pi OS's own kernel and its Chromium build | ❌ Debian's; booting the real image in a VM (QEMU) would come closer |

So it answers "does it install, start and work on the Pi's software", not "is it fast enough on the Pi's hardware"
or "does the mic work". On the Pi 4 profile at its speed, for example, TARS's whole test suite passes in about half
a minute, and the browser agent did the nine tasks that need no account, about twice as slowly as on a Mac
([browser-evals.md](browser-evals.md#on-a-simulated-pi-4-2026-10-24)).

## Reaching it

A service started on the device is reached from this machine through the ports `SIM_PORTS` names, on 127.0.0.1
only (the device is made again when they change, its volume kept):

```bash
SIM_PORTS="8080" deploy/sim/run.sh uv run voice-assistant --web --host 0.0.0.0   # the web UI at localhost:8080
```

## Devices

A device is a profile in [`deploy/sim/devices/`](../deploy/sim/devices/), a few lines:

```bash
DESCRIPTION="Raspberry Pi 4, 8 GB, Raspberry Pi OS Lite (Debian bookworm, arm64)"
PLATFORM=linux/arm64        # the container's architecture
BASE=debian:bookworm        # the image its OS comes from
CPUS=4                      # its cores and memory, as far as the engine has them
MEMORY_MB=8192
SPEED_CPUS=1.2              # --speed: this machine's CPUs that do about as much as the device's
PACKAGES=pi-packages.txt    # the apt packages it gets, from deploy/
USER_NAME=pi                # the user TARS runs as there
```

`pi4` (the default, the household's) and `pi5` are there. Another, say a mini PC on Debian trixie, is another file:
`PLATFORM=linux/amd64`, `BASE=debian:trixie`, and its own `SPEED_CPUS`, measured by running the same thing on both
and comparing.
