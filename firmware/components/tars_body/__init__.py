"""TARS's body link (docs/bodies.md) for an ESP32: the device hears "hey TARS" with its own small model, sends the
seconds that had the name in them to the brain, which double-checks them, then streams what's said and plays the
reply. Its key signs with Monocypher (monocypher*.c, BSD-2-Clause or CC0, 4.0.2). The body's half of the protocol
is voice_assistant.bodyclient.BodyProtocol, which this follows."""

import base64
import wave
from pathlib import Path

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import esp32, microphone, speaker
from esphome.components.micro_wake_word import MicroWakeWord
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_SPEAKER, CONF_URL
from esphome.core import HexInt

AUTO_LOAD = ["json", "ring_buffer", "audio"]
DEPENDENCIES = ["microphone", "speaker", "micro_wake_word", "wifi"]
CODEOWNERS = []

CONF_KEY = "key"
CONF_WAKE_WORD = "micro_wake_word"
CONF_UNREACHABLE = "unreachable"
CONF_HOW = "how"
CONF_SCORE = "score"
KEY_PREFIX = "tars-body-1"

tars_body_ns = cg.esphome_ns.namespace("tars_body")
TarsBody = tars_body_ns.class_("TarsBody", cg.Component)
WakeAction = tars_body_ns.class_("WakeAction", automation.Action, cg.Parented.template(TarsBody))
PressAction = tars_body_ns.class_("PressAction", automation.Action, cg.Parented.template(TarsBody))


def _key(value):
    """`tars-body-1:<device>:<key>`, the line `voice-assistant --body-add ROOM` prints (bodykeys.key_text)."""
    value = cv.string_strict(value).strip()
    prefix, _, rest = value.partition(":")
    device, _, encoded = rest.partition(":")
    if prefix != KEY_PREFIX or not device or not encoded:
        raise cv.Invalid(f"not a body's key (it starts with {KEY_PREFIX}:)")
    try:
        seed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError:
        raise cv.Invalid("a body's key, damaged") from None
    if len(seed) != 32:
        raise cv.Invalid("a body's key, damaged")
    return value


def _url(value):
    value = cv.string_strict(value)
    if not value.startswith(("ws://", "wss://")):
        raise cv.Invalid("the brain's address starts with ws:// or wss://, like ws://homepi.local:8766")
    return value


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(TarsBody),
        cv.Required(CONF_URL): _url,
        cv.Required(CONF_KEY): _key,
        cv.Required(CONF_WAKE_WORD): cv.use_id(MicroWakeWord),
        cv.Optional(CONF_MICROPHONE, default={}): microphone.microphone_source_schema(16, 16, 1, 1),
        # Plays 16-bit mono at the brain's rate (24 kHz): a resampler in front of the board's speaker.
        cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
        # Said when someone calls TARS and the brain can't be reached: a 16-bit mono WAV at 24 kHz.
        cv.Optional(CONF_UNREACHABLE): cv.file_,
    }
).extend(cv.COMPONENT_SCHEMA)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {cv.Optional(CONF_MICROPHONE): microphone.final_validate_microphone_source_schema("tars_body", sample_rate=16000)},
    extra=cv.ALLOW_EXTRA,
)


def _seed(key: str) -> bytes:
    encoded = key.split(":", 2)[2]
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    cg.add(var.set_url(config[CONF_URL]))
    key = config[CONF_KEY]
    cg.add(var.set_device(key.split(":", 2)[1]))
    seed_id = cv.declare_id(cg.uint8)(f"{config[CONF_ID].id}_seed")
    cg.add(var.set_seed(cg.static_const_array(seed_id, [HexInt(b) for b in _seed(key)])))

    cg.add(var.set_wake_word(await cg.get_variable(config[CONF_WAKE_WORD])))
    # Hears what the wake word hears, while it runs: muted, the microphone is off for both.
    cg.add(var.set_microphone_source(await microphone.microphone_source_to_code(config[CONF_MICROPHONE], passive=True)))
    cg.add(var.set_speaker(await cg.get_variable(config[CONF_SPEAKER])))

    if path := config.get(CONF_UNREACHABLE):
        with wave.open(str(Path(path)), "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 24000):
                raise cv.Invalid(f"{path}: needs 16-bit mono at 24 kHz")
            pcm = w.readframes(w.getnframes())
        line_id = cv.declare_id(cg.uint8)(f"{config[CONF_ID].id}_unreachable")
        cg.add(var.set_unreachable(cg.progmem_array(line_id, [HexInt(b) for b in pcm]), len(pcm)))

    esp32.add_idf_component(name="espressif/esp_websocket_client", ref="1.6.0")
    esp32.request_tls()  # esp_websocket_client links esp_tls even for ws://


@automation.register_action(
    "tars_body.wake",
    WakeAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(TarsBody),
            cv.Optional(CONF_HOW, default="word"): cv.one_of("word", "button", lower=True),
            cv.Optional(CONF_SCORE, default=1.0): cv.templatable(cv.float_),
        }
    ),
)
async def wake_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_button(config[CONF_HOW] == "button"))
    cg.add(var.set_score(await cg.templatable(config[CONF_SCORE], args, float)))
    return var


@automation.register_action("tars_body.press", PressAction, cv.Schema({cv.GenerateID(): cv.use_id(TarsBody)}))
async def press_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var
