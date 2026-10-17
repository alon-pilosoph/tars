"""config.toml: the shipped one loads, and a setting that can't work stops the assistant with one clear line."""

from pathlib import Path

import pytest

from voice_assistant.config import OPENAI_STT_MODEL, Config, ConfigError, load_config, with_keys

REPO_CONFIG = Path(__file__).parents[1] / "config.toml"


def config(tmp_path, text: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return load_config(path)


def test_repo_config_loads_with_every_section():
    cfg = load_config(REPO_CONFIG)
    assert isinstance(cfg, Config)
    assert cfg.tts.provider == "cartesia" and cfg.tts.cartesia_voice and cfg.recorder.follow_up_s > 0


def test_the_shipped_config_uses_the_system_s_audio_devices():
    cfg = load_config(REPO_CONFIG)
    assert cfg.audio.input_device == "" and cfg.audio.output_device == ""


def test_missing_config_falls_back_to_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.speaker.enabled is False and cfg.tts.effect == ""


@pytest.mark.parametrize(
    "setting, says",
    [
        ('[stt]\nprovider = "whisper"', "[stt] provider"),
        ("[recorder]\nmax_pause_s = 0.5", "max_pause_s"),
        ("[recorder]\nanswer_early_s = 0.9", "answer_early_s"),
        ("[recorder]\nvad_threshold = 0.1", "vad_threshold"),
        ('[wake]\nmode = "push_to_talk"', "[wake] mode"),
        ('[tts]\neffect = "robot"', "[tts] effect"),
        ('[stt]\nprovider = "flux"\nlanguage = "he"', "English"),
        ('[stt]\nprovider = "deepgram"\nmodel = "gpt-4o-mini-transcribe"', "Deepgram model"),
        ('[tts]\ndeepgram_host = "https://api.eu.deepgram.com"', "[tts] deepgram_host"),
        ('[tts]\nprovider = "cartesia"\nmodel = "aura-2-zeus-en"\ncartesia_voice = "x"', "Cartesia model"),
        ('[tts]\nprovider = "cartesia"\nmodel = "sonic-3.6"', "cartesia_voice"),
        ("[recorder]\nend_silence = 0.8", "[recorder] has no setting end_silence"),
        ('[recorder]\nend_silence_s = "0.8"', "[recorder] end_silence_s must be a number"),
        ("[speaker]\nenabled = 1", "[speaker] enabled must be true or false"),
        ("[speakers]\nenabled = true", "no [speakers] section"),
        ("[audio\n", "config.toml"),  # not TOML at all
    ],
)
def test_a_setting_that_cannot_work_stops_at_startup_with_one_line(tmp_path, setting, says):
    with pytest.raises(SystemExit) as stopped:
        config(tmp_path, setting)
    message = str(stopped.value)
    assert says in message and "\n" not in message


def test_a_whole_number_is_a_fine_number(tmp_path):
    assert config(tmp_path, "[recorder]\nmax_utterance_s = 20").recorder.max_utterance_s == 20


EVERY_KEY = {"OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"}


def test_with_every_key_the_setup_runs_as_config_toml_says():
    cfg = load_config(REPO_CONFIG)
    assert with_keys(cfg, EVERY_KEY) == (cfg, [])


def test_without_groqs_key_qwen_runs_on_cerebras_alone():
    cfg, notes = with_keys(load_config(REPO_CONFIG), EVERY_KEY - {"GROQ_API_KEY"})
    assert cfg.llm.groq_model == "" and cfg.llm.cerebras_model
    assert notes == ["no GROQ_API_KEY: Qwen doesn't run on Groq"]


def test_without_deepgram_openai_hears_and_cartesia_still_speaks():
    shipped = load_config(REPO_CONFIG)
    cfg, notes = with_keys(shipped, EVERY_KEY - {"DEEPGRAM_API_KEY"})
    assert (cfg.stt.provider, cfg.stt.model) == ("openai", OPENAI_STT_MODEL)
    assert cfg.tts == shipped.tts and len(notes) == 1 and cfg.llm == shipped.llm


def test_without_deepgram_or_cartesia_openai_hears_and_speaks():
    shipped = load_config(REPO_CONFIG)
    cfg, notes = with_keys(shipped, EVERY_KEY - {"DEEPGRAM_API_KEY", "CARTESIA_API_KEY"})
    assert (cfg.stt.provider, cfg.tts.provider, cfg.tts.model) == ("openai", "openai", "gpt-4o-mini-tts")
    assert cfg.tts.voice == shipped.tts.voice and len(notes) == 2


def test_without_openai_qwen_answers_everything_and_deepgram_hears_and_speaks():
    shipped = load_config(REPO_CONFIG)
    cfg, notes = with_keys(shipped, EVERY_KEY - {"OPENAI_API_KEY"})
    assert not cfg.llm.web_search and not cfg.llm.think_effort and cfg.llm.send == cfg.llm.quick_tools
    assert cfg.stt == shipped.stt and cfg.tts == shipped.tts and "Qwen answers everything" in notes[0]


def test_without_openai_its_speech_to_text_and_voice_move_to_deepgram(tmp_path):
    setup = '[stt]\nprovider = "openai"\n[tts]\nprovider = "openai"\n[llm]\ncerebras_model = "qwen"'
    cfg, _ = with_keys(config(tmp_path, setup), {"DEEPGRAM_API_KEY", "CEREBRAS_API_KEY"})
    assert (cfg.stt.provider, cfg.tts.provider) == ("flux", "deepgram") and cfg.tts.model.startswith("flux-")


CARTESIA = '[tts]\nprovider = "cartesia"\nmodel = "sonic-3.6"\ncartesia_voice = "a-voice-id"'


def test_without_cartesia_deepgram_speaks_and_without_either_openai_does(tmp_path):
    cartesia = config(tmp_path, CARTESIA)
    assert with_keys(cartesia, EVERY_KEY) == (cartesia, [])
    cfg, notes = with_keys(cartesia, EVERY_KEY - {"CARTESIA_API_KEY"})
    assert (cfg.tts.provider, cfg.tts.model) == ("deepgram", "flux-wes-en") and "CARTESIA_API_KEY" in notes[0]
    cfg, _ = with_keys(cartesia, EVERY_KEY - {"DEEPGRAM_API_KEY", "CARTESIA_API_KEY"})
    assert (cfg.tts.provider, cfg.tts.model, cfg.tts.voice) == ("openai", "gpt-4o-mini-tts", cartesia.tts.voice)


@pytest.mark.parametrize(
    "keys, says",
    [
        ({"GROQ_API_KEY", "CEREBRAS_API_KEY"}, "Hearing and speaking need"),
        ({"DEEPGRAM_API_KEY"}, "The brain needs"),
    ],
)
def test_a_stage_with_no_service_left_stops_at_startup_naming_the_keys(keys, says):
    with pytest.raises(ConfigError, match=says):
        with_keys(load_config(REPO_CONFIG), keys)


@pytest.mark.parametrize("country, ok", [("IL", True), ("", True), ("il", False), ("Israel", False)])
def test_home_country_is_a_two_letter_code(tmp_path, country, ok):
    setup = f'[llm]\nhome_city = "Tel Aviv"\nhome_country = "{country}"'
    if ok:
        assert config(tmp_path, setup).llm.home_country == country
    else:
        with pytest.raises(ConfigError, match="two-letter country code"):
            config(tmp_path, setup)
