"""config.toml: the shipped one loads, and a setting that can't work stops the assistant with one clear line."""

import re
from pathlib import Path

import pytest

from voice_assistant.config import OPENAI_STT_MODEL, Config, ConfigError, load_config, with_keys

REPO_CONFIG = Path(__file__).parents[1] / "config.toml"


def config(tmp_path, text: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return load_config(path)


def test_repo_config_loads_with_every_section():
    cfg = load_config(REPO_CONFIG, local=False)
    assert isinstance(cfg, Config)
    assert cfg.tts.provider == "cartesia" and cfg.tts.cartesia_voice and cfg.recorder.follow_up_s > 0


def test_the_shipped_config_uses_the_system_s_audio_devices():
    cfg = load_config(REPO_CONFIG, local=False)
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
        ("[telegram]\nenabled = true", "[telegram] people"),
        ('[telegram]\npeople = { Alon = "123" }', "[telegram] people must be a table"),
        ("[telegram]\npeople = { Alon = 1, Stacey = 1 }", "the same chat id twice"),
        ('[telegram]\npeople = { " " = 1 }', "a name for each chat id"),
        ("[telegram]\nmemory_minutes = 0", "memory_minutes must be more than 0"),
        ("[backup]\nenabled = true", "[backup] bucket must name"),
        ('[backup]\nenabled = true\nbucket = "tars-home"', "[backup] region must say"),
        ('[backup]\nbucket = "Tars_Home"', "must be an S3 bucket name"),
        ('[backup]\nregion = "europe"', "must be an AWS region"),
        ('[backup]\nprefix = "/tars/"', "no slash at either end"),
        ("[backup]\nkeep_snapshots_days = 0", "keep_snapshots_days must be at least 1"),
        ("[browser]\nenabled = true", "[browser] needs [telegram]"),
        ('[browser]\neffort = "max"', "[browser] effort must be one of"),
        ("[browser]\nturns = 2", "[browser] turns must be between 5 and 100"),
        ("[browser]\nlogin_port = 80", "[browser] login_port must be between 1024 and 65535"),
        ('[browser]\nmodel = "claude-opus-5-5"', "[browser] model must be an OpenAI model"),
    ],
)
def test_a_setting_that_cannot_work_stops_at_startup_with_one_line(tmp_path, setting, says):
    with pytest.raises(SystemExit) as stopped:
        config(tmp_path, setting)
    message = str(stopped.value)
    assert says in message and "\n" not in message


def test_telegram_people_are_names_and_chat_ids(tmp_path):
    cfg = config(tmp_path, "[telegram]\nenabled = true\npeople = { Alon = 123456789 }")
    assert cfg.telegram.people == {"Alon": 123456789} and cfg.telegram.send_items


def test_a_whole_number_is_a_fine_number(tmp_path):
    assert config(tmp_path, "[recorder]\nmax_utterance_s = 20").recorder.max_utterance_s == 20


EVERY_KEY = {"OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"}


def test_with_every_key_the_setup_runs_as_config_toml_says():
    cfg = load_config(REPO_CONFIG, local=False)
    assert with_keys(cfg, EVERY_KEY) == (cfg, [])


def test_without_groqs_key_qwen_runs_on_cerebras_alone():
    cfg, notes = with_keys(load_config(REPO_CONFIG, local=False), EVERY_KEY - {"GROQ_API_KEY"})
    assert cfg.llm.groq_model == "" and cfg.llm.cerebras_model
    assert notes == ["no GROQ_API_KEY: Qwen doesn't run on Groq"]


def test_without_deepgram_openai_hears_and_cartesia_still_speaks():
    shipped = load_config(REPO_CONFIG, local=False)
    cfg, notes = with_keys(shipped, EVERY_KEY - {"DEEPGRAM_API_KEY"})
    assert (cfg.stt.provider, cfg.stt.model) == ("openai", OPENAI_STT_MODEL)
    assert cfg.tts == shipped.tts and len(notes) == 1 and cfg.llm == shipped.llm


def test_without_deepgram_or_cartesia_openai_hears_and_speaks():
    shipped = load_config(REPO_CONFIG, local=False)
    cfg, notes = with_keys(shipped, EVERY_KEY - {"DEEPGRAM_API_KEY", "CARTESIA_API_KEY"})
    assert (cfg.stt.provider, cfg.tts.provider, cfg.tts.model) == ("openai", "openai", "gpt-4o-mini-tts")
    assert cfg.tts.voice == shipped.tts.voice and len(notes) == 2


def test_without_openai_qwen_answers_everything_and_deepgram_hears_and_speaks():
    shipped = load_config(REPO_CONFIG, local=False)
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
        with_keys(load_config(REPO_CONFIG, local=False), keys)


@pytest.mark.parametrize("country, ok", [("IL", True), ("", True), ("il", False), ("Israel", False)])
def test_home_country_is_a_two_letter_code(tmp_path, country, ok):
    setup = f'[llm]\nhome_city = "Tel Aviv"\nhome_country = "{country}"'
    if ok:
        assert config(tmp_path, setup).llm.home_country == country
    else:
        with pytest.raises(ConfigError, match="two-letter country code"):
            config(tmp_path, setup)


@pytest.mark.parametrize(
    "setup, says",
    [
        ("[wake]\nthreshold = 1.5", "[wake] threshold must be more than 0 and less than 1"),
        ("[wake]\nthreshold = 0", "[wake] threshold must be more than 0 and less than 1"),
        ("[wake]\ncheck_window_s = 0", "[wake] check_window_s must be more than 0"),
        ("[speaker]\nthreshold = 50", "[speaker] threshold must be more than 0 and at most 1"),
        ("[speaker]\nwake_threshold = -0.1", "[speaker] wake_threshold must be more than 0 and at most 1"),
        ("[recorder]\nstart_timeout_s = 0", "[recorder] start_timeout_s must be more than 0"),
        ("[recorder]\nmax_utterance_s = -1", "[recorder] max_utterance_s must be more than 0"),
        ("[recorder]\nfollow_up_s = -1", "[recorder] follow_up_s must be 0 (off) or more"),
        ("[recorder]\ngreet_after_s = -1", "[recorder] greet_after_s must be 0 (off) or more"),
        ("[llm]\nhumor = 150", "[llm] humor must be a percentage"),
        ("[llm]\nmemory_minutes = -5", "[llm] memory_minutes must be 0 or more"),
    ],
)
def test_a_setting_out_of_its_range_stops_at_startup_saying_which(tmp_path, setup, says):
    with pytest.raises(ConfigError, match=re.escape(says)):
        config(tmp_path, setup)


def test_switching_off_what_zero_switches_off_is_fine(tmp_path):
    cfg = config(tmp_path, "[recorder]\nfollow_up_s = 0\ngreet_after_s = 0\n[llm]\nhumor = 0\nmemory_minutes = 0")
    assert (cfg.recorder.follow_up_s, cfg.recorder.greet_after_s, cfg.llm.humor) == (0, 0, 0)


def test_this_machines_own_settings_go_on_top_of_the_shared_file(tmp_path):
    (tmp_path / "config.local.toml").write_text(
        '[audio]\ninput_device = "PowerConf"\n[telegram]\nenabled = true\npeople = { Alon = 1 }\n'
    )
    cfg = config(tmp_path, '[audio]\ninput_device = "MacBook"\noutput_device = "Speakers"\n')
    assert (cfg.audio.input_device, cfg.audio.output_device) == ("PowerConf", "Speakers")  # the rest stays
    assert cfg.telegram.enabled and cfg.telegram.people == {"Alon": 1}
    assert load_config(tmp_path / "config.toml", local=False).audio.input_device == "MacBook"


def test_a_mistake_in_the_local_file_says_so(tmp_path):
    (tmp_path / "config.local.toml").write_text("[telegram]\nenabled = true\n")
    with pytest.raises(SystemExit) as stopped:
        config(tmp_path, "")
    assert "config.local.toml" in str(stopped.value) and "[telegram] people" in str(stopped.value)
    (tmp_path / "config.local.toml").write_text("[audio\n")
    with pytest.raises(SystemExit, match="config.local.toml"):
        config(tmp_path, "")
