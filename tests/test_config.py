"""config.toml: the shipped one loads, and a setting that can't work stops the assistant with one clear line."""

from pathlib import Path

import pytest

from voice_assistant.config import Config, load_config, required_keys

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


@pytest.mark.parametrize(
    "setting, keys",
    [
        ('[stt]\nprovider = "openai"\n[tts]\nprovider = "openai"', ["OPENAI_API_KEY"]),
        ('[stt]\nprovider = "flux"', ["OPENAI_API_KEY", "DEEPGRAM_API_KEY"]),
        ('[tts]\nprovider = "deepgram"\nmodel = "aura-2-zeus-en"', ["OPENAI_API_KEY", "DEEPGRAM_API_KEY"]),
        ('[llm]\ncerebras_model = "qwen"', ["OPENAI_API_KEY", "CEREBRAS_API_KEY"]),
    ],
)
def test_only_the_keys_the_setup_uses_are_required(tmp_path, setting, keys):
    assert required_keys(config(tmp_path, setting)) == keys
