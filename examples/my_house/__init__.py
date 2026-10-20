"""One household's own devices, as a TARS plugin: an Electra AC with no Wi-Fi, a Samsung TV and a Polk soundbar, all
through a Tuya IR blaster, and a Fire TV that plays a title on Stremio straight from its link. README.md says
what each is and how it was set up; docs/home.md#your-own-devices says how a plugin works.

In config.toml (or config.local.toml):

    [home.plugins.my_house]
    module = "examples.my_house"
    blaster = { id = "bf0123456789abcdef", address = "192.168.1.50", version = "3.5", control_type = 2 }

    [[home.devices]]
    name = "AC"
    kind = "my_house.ac"
    state = "binary_sensor.ac_light"      # optional: a light sensor on the AC, through Home Assistant

    [[home.devices]]
    name = "TV"
    kind = "my_house.remote"
    codes = "samsung-tv"                  # or polk-re9641
    awake_from = "fire tv"                # optional: the box that says whether the TV is on

    [[home.devices]]
    name = "fire tv"
    kind = "my_house.firetv"
    address = "192.168.1.60"
"""

from collections.abc import Callable

from voice_assistant.androidtv import KEY_FILE, AndroidTV
from voice_assistant.found import FOUND_FILE, Seen
from voice_assistant.home import Context, Device, HomeError

from .ac import AC, FOLDER, PROFILE, STATE, Profile
from .blaster import TUYA_KEY, Blaster, check_spec
from .direct_play import STREMIO_RULES, Catalog, FireTv
from .ir import CODE_SETS, Remote, tested_path


def setup(settings: dict, context: Context) -> dict[str, Callable[[dict], Device]]:
    """The kinds of device this plugin makes, from [home.plugins.my_house]'s settings: `blaster`, the IR blaster
    that the AC and the remotes send through."""
    if unknown := set(settings) - {"blaster"}:
        raise ValueError(f"[home.plugins.my_house] has no setting {', '.join(sorted(unknown))}")
    made: list[Blaster] = []

    def blaster() -> Blaster:
        """Made once, when a device first needs it, so a house with only the Fire TV needs no blaster."""
        if not made:
            spec = settings.get("blaster")
            if spec is None:
                raise ValueError("it goes through the IR blaster: set blaster in [home.plugins.my_house]")
            if problem := check_spec(spec):
                raise ValueError(problem)
            key = context.secret(TUYA_KEY)
            if not key:
                raise ValueError(f"no {TUYA_KEY} in .env: the blaster's local key (examples/my_house/README.md)")
            try:
                import tinytuya  # noqa: F401 - only whether it's there
            except ImportError:
                raise ValueError('the IR blaster needs the optional extra "my-house": uv sync --all-extras') from None
            made.append(Blaster.from_spec(spec, key, Seen(context.root / FOUND_FILE)))
        return made[0]

    def ac(spec: dict) -> AC:
        _only(spec, {"state"})
        sensor = spec.get("state", "")
        if sensor and context.hass is None:
            raise ValueError("its state sensor is read through Home Assistant: set [home] url and the token")
        b = blaster()
        folder = context.root / FOLDER
        profile = Profile.load(folder / PROFILE)
        return AC(spec["name"], profile, b.send, folder / STATE, context.hass, sensor, reachable=b.reachable)

    def remote(spec: dict) -> Remote:
        _only(spec, {"codes", "awake_from"})
        if spec.get("codes") not in CODE_SETS:
            raise ValueError(f"codes must be one of {', '.join(CODE_SETS)}")
        b, box = blaster(), spec.get("awake_from")

        def seems_on() -> bool | None:
            """Whether the TV is on, as its box says (it wakes and sleeps with the TV); None if it can't tell."""
            device = context.devices.get(str(box).strip().lower())
            try:
                return device.box.awake() if device is not None and hasattr(device, "box") else None
            except HomeError:
                return None

        name = spec["name"]
        codes, on = CODE_SETS[spec["codes"]], seems_on if box else None
        return Remote(name, codes, b.send, tested=tested_path(context.root, name), seems_on=on, reachable=b.reachable)

    def firetv(spec: dict) -> FireTv:
        _only(spec, {"address"})
        if not (isinstance(spec.get("address"), str) and spec["address"].strip()):
            raise ValueError("it needs address, its IP address on the home network")
        try:
            import adb_shell  # noqa: F401 - only whether it's there
        except ImportError:
            raise ValueError('it needs the optional extra "tv": uv sync --all-extras') from None
        box = AndroidTV(spec["address"], context.root / KEY_FILE, seen=Seen(context.root / FOUND_FILE))
        agent = context.tv_agent(box, extra_rules=STREMIO_RULES) if context.tv_agent else None
        return FireTv(spec["name"], box, agent, Catalog())

    return {"ac": ac, "remote": remote, "firetv": firetv}


def _only(spec: dict, allowed: set[str]) -> None:
    if unknown := set(spec) - {"name", "kind", *allowed}:
        raise ValueError(f"it has no setting {', '.join(sorted(unknown))}")
