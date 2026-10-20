"""The Tuya IR blaster that sends the AC's, the TV's and the soundbar's codes: on the home network, through tinytuya,
with no cloud. Its local key is TUYA_LOCAL_KEY in .env (README.md says how to get it)."""

from pathlib import Path

from voice_assistant.found import FOUND_FILE, Seen, reachable
from voice_assistant.home import Check, HomeError

TUYA_KEY = "TUYA_LOCAL_KEY"
KEYS = ("id", "address", "version", "control_type")
VERSIONS = ("3.1", "3.2", "3.3", "3.4", "3.5")
# A blaster that doesn't answer is given up on in seconds, so a voice turn says so rather than going quiet: tinytuya's
# defaults (5 s, five tries, 5 s apart) took a minute.
BLASTER_TIMEOUT_S, BLASTER_TRIES, BLASTER_RETRY_S = 2.0, 2, 0.3
TUYA_PORT, REACH_TIMEOUT_S = 6668, 1.5


class Blaster:
    """The Tuya IR blaster, on the home network (tinytuya: local, no cloud)."""

    def __init__(
        self,
        device_id: str,
        address: str,
        local_key: str,
        version: str = "3.3",
        control_type: int = 0,
        seen: Seen | None = None,
    ):
        """`control_type`: which of Tuya's two IR protocols it speaks; 0 = found on first use, from its status, which
        some blasters never give. `seen`: where it was last found, so that a blaster the router gave another address
        is found again by its id (voice_assistant/found.py); None: only ever at `address`."""
        self._id, self._key, self._version = device_id, local_key, float(version)
        self._seen, self._seen_key = seen, f"tuya:{device_id}"
        self._address = (seen.get(self._seen_key).get("address") if seen else None) or address
        self._control_type = control_type
        self._reachable: Check | None = None

    @classmethod
    def from_spec(cls, spec: dict, local_key: str, seen: Seen | None = None) -> Blaster:
        """From [home.plugins.my_house] blaster."""
        version, control_type = spec.get("version", "3.3"), spec.get("control_type", 0)
        return cls(spec["id"], spec["address"], local_key, version, control_type, seen)

    def _locate(self) -> None:
        """One quick look first: finding out which of Tuya's protocols it speaks takes three tries, each with its own
        timeout, before a blaster that's off the Wi-Fi is given up on. Not there: it's looked for by its id, which it
        announces on the home network every few seconds."""
        if reachable(self._address, TUYA_PORT, REACH_TIMEOUT_S):
            return
        found = find_by_id(self._id) if self._seen is not None else None
        if not found or not reachable(found, TUYA_PORT, REACH_TIMEOUT_S):
            raise HomeError(f"the IR blaster didn't answer at {self._address}, and isn't announcing itself elsewhere")
        print(
            f"(home: the IR blaster moved from {self._address} to {found}: the router gave it a new address)",
            flush=True,
        )
        self._address = found
        self._seen.put(self._seen_key, address=found)

    def _device(self):
        from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice

        self._locate()

        ir = IRRemoteControlDevice(
            self._id,
            self._address,
            self._key,
            version=self._version,
            control_type=self._control_type or None,
            connection_timeout=BLASTER_TIMEOUT_S,
            connection_retry_limit=BLASTER_TRIES,
            connection_retry_delay=BLASTER_RETRY_S,
        )
        if not ir.control_type:
            raise HomeError(
                f"the IR blaster at {self._address} didn't say which IR protocol it speaks: set control_type in "
                "[home.plugins.my_house] blaster (README.md)"
            )
        self._control_type = ir.control_type
        return ir

    def connect(self) -> None:
        """Raises HomeError if it doesn't answer."""
        self._device()

    def send(self, pulses: list[int]) -> None:
        from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice

        try:
            result = self._device().send_button(IRRemoteControlDevice.pulses_to_base64(pulses))
        except OSError as e:
            raise HomeError(f"the IR blaster didn't answer ({type(e).__name__})") from e
        if isinstance(result, dict) and "Error" in result:
            raise HomeError(f"the IR blaster said: {result['Error']}")

    def learn(self, timeout_s: float = 20.0) -> list[int] | None:
        """The next code a remote sends it, as marks and spaces, or None if nothing came."""
        from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice

        code = self._device().receive_button(timeout=timeout_s)
        if not isinstance(code, str):
            return None
        return IRRemoteControlDevice.base64_to_pulses(code)

    def reachable(self) -> Check:
        """For --check: whether it answers, asked once however many devices it sends for."""
        if self._reachable is None:
            fix = "is it powered, on the Wi-Fi, at that address? (uv run python -m tinytuya scan)"
            try:
                self.connect()
                self._reachable = Check("ok", f"the IR blaster answers at {self._address}")
            except HomeError as e:
                self._reachable = Check("fail", str(e), fix)
        return self._reachable


def find_by_id(device_id: str) -> str | None:
    """Where the Tuya device with this id is announcing itself now (up to tinytuya's scan time, 18 s; it stops as soon
    as the id is heard), or None."""
    import tinytuya

    return tinytuya.find_device(dev_id=device_id).get("ip")


def check_spec(spec) -> str:
    """What's wrong with [home.plugins.my_house] blaster, or ""."""
    if not isinstance(spec, dict):
        return 'blaster must be a table, like { id = "bf01...", address = "192.168.1.50", version = "3.3" }'
    if unknown := set(spec) - set(KEYS):
        return f"blaster has no setting {', '.join(sorted(unknown))}"
    if not all(isinstance(spec.get(k), str) and spec[k] for k in ("id", "address")):
        return "blaster needs its id and address, as text"
    if spec.get("version", "3.3") not in VERSIONS:
        return f"blaster version must be one of {', '.join(VERSIONS)}"
    if spec.get("control_type", 1) not in (1, 2):
        return "blaster control_type must be 1 (DPS 201/202) or 2 (DPS 1-13)"
    return ""


def from_config(config: Path) -> Blaster:
    """The blaster, as config.toml (and config.local.toml) set it, for the command-line tools here."""
    from dotenv import dotenv_values

    from voice_assistant.config import load_config

    plugin = next((p for p in load_config(config).home.plugins.values() if p.get("module") == __package__), None)
    spec = (plugin or {}).get("blaster")
    if not spec:
        raise SystemExit(f"config.toml has no [home.plugins] with module = {__package__!r} and a blaster: README.md")
    if problem := check_spec(spec):
        raise SystemExit(f"[home.plugins]: {problem}")
    key = dotenv_values(config.resolve().parent / ".env").get(TUYA_KEY)
    if not key:
        raise SystemExit(f"Put the blaster's local key in .env as {TUYA_KEY} (README.md says how to get it)")
    return Blaster.from_spec(spec, key, Seen(config.resolve().parent / FOUND_FILE))
