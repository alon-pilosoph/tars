"""One household's own devices, as a TARS plugin: an Electra AC with no Wi-Fi, a Samsung TV and a Polk soundbar, all
through a Tuya IR blaster, and a Fire TV that plays a title on Stremio straight from its link. README.md says
what each is and how it was set up; docs/home.md#your-own-devices says how a plugin works.

In config.toml (or config.local.toml):

    [home.plugins.my_house]
    module = "examples.my_house"
    grant = ["TUYA_LOCAL_KEY", "home_assistant", "tv_agent", "devices", "announce", "tell"]
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
    tv_address = "192.168.1.61"           # optional, with tv_input: TARS turns the TV and soundbar on as needed
    tv_input = "hdmi4"                    # the TV's button for the Fire TV's input
    youtube = "smarttube"                 # optional: YouTube's links play in SmartTube, sideloaded, not YouTube's app

    [[home.devices]]
    name = "air purifier"
    kind = "my_house.purifier"
    address = "48:87:2D:00:00:00"         # its Bluetooth address: a Soehnle AC500, from this machine's own adapter
"""

from collections.abc import Callable

from voice_assistant.androidtv import KEY_FILE, AndroidTV
from voice_assistant.found import FOUND_FILE, Seen
from voice_assistant.home import Context, Device, HomeError
from voice_assistant.plugins import Manifest

from .ac import AC, FOLDER, PROFILE, STATE, Profile
from .blaster import TUYA_KEY, Blaster, check_spec
from .direct_play import STREMIO_RULES, Catalog, FireTv
from .ir import CODE_SETS, TESTED, Remote, tested_path
from .purifier import Bluetooth, Purifier, Watch
from .screen import Screen

# What it needs, to be granted under [home.plugins.my_house] grant: the blaster's key, Home Assistant (the AC's light
# sensor), the Android TV agent, the other devices (a remote asks the Fire TV whether the TV is on), saying aloud
# what the Fire TV did, and telling the maintainers when the purifier stops answering. Never unlock.
PLUGIN = Manifest(version="1.0.0", needs=(TUYA_KEY, "home_assistant", "tv_agent", "devices", "announce", "tell"))

# What this house has learned, kept in the backup's snapshot: the AC's profile, its presses and what it was last sent,
# and which power codes each remote's set answered to.
BACKUP_FILES = (FOLDER, TESTED)


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

        def power_by(on: bool) -> str | None:
            """Through the box, if it has the screen: the TV, the soundbar and the box together."""
            device = context.devices.get(str(box).strip().lower())
            return device.power(on) if isinstance(device, FireTv) else None

        name = spec["name"]
        codes, on = CODE_SETS[spec["codes"]], seems_on if box else None
        tested = tested_path(context.root, name)
        return Remote(
            name, codes, b.send, tested=tested, seems_on=on, reachable=b.reachable, power_by=power_by if box else None
        )

    def firetv(spec: dict) -> FireTv:
        _only(spec, {"address", "tv_address", "tv_input", "tv", "soundbar", "youtube"})
        if not (isinstance(spec.get("address"), str) and spec["address"].strip()):
            raise ValueError("it needs address, its IP address on the home network")
        try:
            import adb_shell  # noqa: F401 - only whether it's there
        except ImportError:
            raise ValueError('it needs the optional extra "tv": uv sync --all-extras') from None
        box = AndroidTV(spec["address"], context.root / KEY_FILE, seen=Seen(context.root / FOUND_FILE))
        agent = context.tv_agent(box, extra_rules=STREMIO_RULES) if context.tv_agent else None
        screen = None
        if spec.get("tv_address"):
            if not spec.get("tv_input"):
                raise ValueError("with tv_address it needs tv_input, the TV's button for its input, like hdmi4")

            def named(key: str, default: str) -> Callable[[], Remote | None]:
                return lambda: context.devices.get(str(spec.get(key, default)).strip().lower())

            tv, soundbar = named("tv", "tv"), named("soundbar", "soundbar")
            screen = Screen(box, tv, soundbar, spec["tv_address"], spec["tv_input"])
        youtube = str(spec.get("youtube", "youtube")).strip().lower()
        if youtube not in ("youtube", "smarttube"):
            raise ValueError('youtube is "youtube" or "smarttube", the app that plays YouTube')
        device = FireTv(spec["name"], box, agent, Catalog(), announce=context.announce or print, screen=screen)
        device.youtube = youtube
        return device

    def purifier(spec: dict) -> Purifier:
        _only(spec, {"address"})
        if not (isinstance(spec.get("address"), str) and spec["address"].strip()):
            raise ValueError("it needs address, its Bluetooth address")
        try:
            import bleak  # noqa: F401 - only whether it's there
        except ImportError:
            raise ValueError('it needs the optional extra "my-house": uv sync --all-extras') from None
        link = Bluetooth(spec["address"].strip())
        if not (context.background and context.tell):
            return Purifier(spec["name"], link)
        # The voice assistant's watches its Bluetooth, and tells the maintainers when it goes quiet.
        watch = Watch(spec["name"], link.heard, context.tell, restart=link.restart).start()
        return Purifier(spec["name"], link, answered=watch.answered)

    return {"ac": ac, "remote": remote, "firetv": firetv, "purifier": purifier}


def _only(spec: dict, allowed: set[str]) -> None:
    if unknown := set(spec) - {"name", "kind", *allowed}:
        raise ValueError(f"it has no setting {', '.join(sorted(unknown))}")
