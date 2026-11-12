"""The systemd units in deploy/: each service sandboxed as docs/security.md#the-services-on-the-machine says, the
updater not."""

import configparser
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parents[1] / "deploy"
BASE = {
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "read-only",
    "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "UMask": "0077",
}
STRICT = {"SystemCallFilter": "@system-service", "CapabilityBoundingSet": "", "ProtectKernelTunables": "yes"}
CHROMIUM_SANDBOX_NEEDS = ("SystemCallFilter", "MemoryDenyWriteExecute", "RestrictNamespaces", "CapabilityBoundingSet")
BUS = {"-/run/user/%U/bus", "-/run/user/%U/systemd"}
DOCKER = {"-/run/docker.sock", "-/var/run/docker.sock"}


def service(name: str) -> dict[str, str]:
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str
    unit.read(DEPLOY / f"{name}.service")
    return dict(unit["Service"])


def hidden(name: str) -> set[str]:
    """Every path InaccessiblePaths= hides, across the unit's lines (configparser keeps only the last of a repeat)."""
    lines = (DEPLOY / f"{name}.service").read_text().splitlines()
    return {p for line in lines if line.startswith("InaccessiblePaths=") for p in line.partition("=")[2].split()}


@pytest.mark.parametrize(
    "name, wanted",
    [
        ("voice-assistant", BASE | STRICT),
        ("voice-assistant-web", BASE | STRICT),
        ("voice-assistant-backup", BASE | STRICT | {"PrivateDevices": "yes"}),
        ("voice-assistant-telegram", BASE),
    ],
)
def test_each_service_is_sandboxed(name, wanted):
    got = service(name)
    assert {k: got.get(k) for k in wanted} == wanted
    assert got["ReadWritePaths"].split()[0] == "%h/voice-assistant"


@pytest.mark.parametrize(
    "name",
    [
        "voice-assistant",  # the sound card, the speakerphone's mute, the IR remote
        "voice-assistant-web",  # /dev/vcio: vcgencmd reads the Pi's power and heat warnings for the Status page
    ],
)
def test_each_service_that_uses_a_device_keeps_them(name):
    assert "PrivateDevices" not in service(name)


@pytest.mark.parametrize("name", sorted(p.stem for p in DEPLOY.glob("*.service")))
def test_no_unit_counts_on_a_device_filter_a_user_manager_cant_load(name):
    # systemd enforces these with a cgroup BPF program, which a manager that isn't root never loads (bpf-devices.c).
    assert not {"DevicePolicy", "DeviceAllow"} & service(name).keys()


def test_telegram_leaves_chromium_its_own_sandbox():
    got = service("voice-assistant-telegram")
    assert [k for k in CHROMIUM_SANDBOX_NEEDS if k in got] == []


@pytest.mark.parametrize("name", ["voice-assistant", "voice-assistant-web", "voice-assistant-backup"])
def test_only_chromium_writes_to_config(name):
    assert not any(p.lstrip("-").startswith("%h/.config") for p in service(name)["ReadWritePaths"].split())


def test_telegram_writes_only_chromiums_config():
    written = [p for p in service("voice-assistant-telegram")["ReadWritePaths"].split() if ".config" in p]
    assert written == ["-%h/.config/chromium", "-%h/.config/chromium-headless"]


@pytest.mark.parametrize(
    "name, without",
    [
        ("voice-assistant", set()),  # restarts itself through systemd-run; Home Assistant's history runs in Docker
        ("voice-assistant-telegram", set()),  # the same, and starts the updater
        ("voice-assistant-web", DOCKER),  # asks systemctl each unit's state
        ("voice-assistant-backup", BUS),  # Home Assistant's history runs in Docker
        ("voice-assistant-display", DOCKER),  # ProtectHome=yes already hides /run/user
    ],
)
def test_each_service_reaches_only_the_managers_it_uses(name, without):
    assert hidden(name) == without


def test_the_screen_writes_only_tmp_and_has_no_home():
    got = service("voice-assistant-display")
    assert (
        got["ProtectHome"] == "yes" and got["ReadWritePaths"] == "/tmp" and got["RestrictAddressFamilies"] == "AF_UNIX"
    )


@pytest.mark.parametrize("name", ["voice-assistant-display", "voice-assistant-telegram"])
def test_the_browser_and_screen_share_a_network_for_the_abstract_socket(name):
    assert "PrivateNetwork" not in service(name)


def test_the_updater_isnt_sandboxed_since_it_installs_as_root():
    assert "NoNewPrivileges" not in service("voice-assistant-update")
