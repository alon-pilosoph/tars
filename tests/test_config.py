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
    assert cfg.tts.effect == "tars" and cfg.recorder.follow_up_s > 0 and cfg.llm.memory_minutes > 0


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


EVERY_KEY = {"OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY"}


def test_with_every_key_the_setup_runs_as_config_toml_says():
    cfg = load_config(REPO_CONFIG)
    assert with_keys(cfg, EVERY_KEY) == (cfg, [])


def test_without_groqs_key_qwen_runs_on_cerebras_alone():
    cfg, notes = with_keys(load_config(REPO_CONFIG), EVERY_KEY - {"GROQ_API_KEY"})
    assert cfg.llm.groq_model == "" and cfg.llm.cerebras_model
    assert notes == ["no GROQ_API_KEY: Qwen doesn't run on Groq"]


def test_without_deepgram_openai_hears_and_speaks():
    shipped = load_config(REPO_CONFIG)
    cfg, notes = with_keys(shipped, EVERY_KEY - {"DEEPGRAM_API_KEY"})
    assert (cfg.stt.provider, cfg.stt.model) == ("openai", OPENAI_STT_MODEL)
    assert (cfg.tts.provider, cfg.tts.model, cfg.tts.voice) == ("openai", "gpt-4o-mini-tts", shipped.tts.voice)
    assert len(notes) == 2 and cfg.llm == shipped.llm


def test_without_openai_qwen_answers_everything_and_deepgram_hears_and_speaks():
    shipped = load_config(REPO_CONFIG)
    cfg, notes = with_keys(shipped, EVERY_KEY - {"OPENAI_API_KEY"})
    assert not cfg.llm.web_search and not cfg.llm.think_effort and cfg.llm.send == cfg.llm.quick_tools
    assert cfg.stt == shipped.stt and cfg.tts == shipped.tts and "Qwen answers everything" in notes[0]


def test_without_openai_its_speech_to_text_and_voice_move_to_deepgram(tmp_path):
    setup = '[stt]\nprovider = "openai"\n[tts]\nprovider = "openai"\n[llm]\ncerebras_model = "qwen"'
    cfg, _ = with_keys(config(tmp_path, setup), {"DEEPGRAM_API_KEY", "CEREBRAS_API_KEY"})
    assert (cfg.stt.provider, cfg.tts.provider) == ("flux", "deepgram") and cfg.tts.model.startswith("aura-2-")


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
