"""The systemd units in deploy/: each service sandboxed as docs/security.md#the-pi says, the updater not."""

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
}
STRICT = {"SystemCallFilter": "@system-service", "CapabilityBoundingSet": "", "ProtectKernelTunables": "yes"}


def service(name: str) -> dict[str, str]:
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str
    unit.read(DEPLOY / f"{name}.service")
    return dict(unit["Service"])


@pytest.mark.parametrize(
    "name, wanted",
    [
        ("voice-assistant", BASE | STRICT),
        ("voice-assistant-web", BASE | STRICT | {"PrivateDevices": "yes"}),
        ("voice-assistant-backup", BASE | STRICT | {"PrivateDevices": "yes"}),
        ("voice-assistant-telegram", BASE),
    ],
)
def test_each_service_is_sandboxed(name, wanted):
    got = service(name)
    assert {k: got.get(k) for k in wanted} == wanted
    assert got["ReadWritePaths"].split()[0] == "%h/voice-assistant"


def test_the_screen_writes_only_its_socket():
    got = service("voice-assistant-display")
    assert (
        got["ProtectHome"] == "yes" and got["ReadWritePaths"] == "/tmp" and got["RestrictAddressFamilies"] == "AF_UNIX"
    )


def test_the_updater_isnt_sandboxed_since_it_installs_as_root():
    assert "NoNewPrivileges" not in service("voice-assistant-update")
