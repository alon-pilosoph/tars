"""Recorder, speech splitting, the TARS effect and config: the parts that don't need a device or an API."""

from pathlib import Path

import numpy as np
import pytest

from voice_assistant.audio import BLOCK_SAMPLES, Microphone, MicrophoneError
from voice_assistant.config import Config, RecorderConfig, load_config
from voice_assistant.effects import EFFECTS, SpeakerBox, VoiceWithEffect, apply_effect
from voice_assistant.enroll import PAD_BLOCKS
from voice_assistant.recorder import UtteranceRecorder
from voice_assistant.speech import clean_for_speech, split_sentences
from voice_assistant.vad import SileroVAD

from .conftest import FakeMic, chime_block, quiet_block

VAD_MODEL = Path(__file__).parents[1] / "models/silero_vad.onnx"


class ScriptedVAD:
    """Speech probabilities from a pattern, one character per block: S speech, m in between, . silence."""

    def __init__(self, pattern):
        self.probs = iter({"S": 0.9, "m": 0.4, ".": 0.05}[c] for c in pattern)

    def __call__(self, block):
        return next(self.probs, 0.05)

    def reset(self):
        pass


def recorder(pattern, turn=None, **cfg):
    return UtteranceRecorder(RecorderConfig(**cfg), ScriptedVAD(pattern), turn)


def mic(n=200):
    return FakeMic([quiet_block() for _ in range(n)])


def blocks_in(pcm: bytes) -> int:
    return len(pcm) // 2 // BLOCK_SAMPLES


def test_recorder_trims_to_the_speech():
    pcm = recorder("." * 20 + "S" * 12 + "." * 40).record(mic())
    assert 12 <= blocks_in(pcm) <= 12 + 8


def test_recorder_keeps_padding_when_asked():
    pattern = "." * 20 + "S" * 12 + "." * 40
    default = recorder(pattern).record(mic())
    padded = recorder(pattern).record(mic(), preroll_blocks=PAD_BLOCKS, tail_blocks=PAD_BLOCKS)
    assert blocks_in(padded) - blocks_in(default) >= 2 * PAD_BLOCKS - 8  # about a second on each side


def test_recorder_gives_up_when_nobody_speaks():
    assert recorder("." * 100).record(mic(), start_timeout_s=1.0) is None


def test_a_lone_speech_block_does_not_start_a_recording():
    assert recorder("..S..S...S" + "." * 40).record(mic(), start_timeout_s=3.0) is None


def test_a_soft_stretch_mid_sentence_does_not_end_it():
    """Once someone's talking, only a clearly quiet block counts as silence (the detector's own hysteresis)."""
    pcm = recorder("SSSS" + "m" * 10 + "SS" + "." * 20, end_silence_s=0.4).record(mic())
    assert blocks_in(pcm) >= 16


class FakeTurn:
    def __init__(self, finished):
        self.p, self.asked = finished, 0

    def finished(self, pcm):
        self.asked += 1
        return self.p


def test_a_finished_sentence_ends_after_the_usual_pause():
    turn = FakeTurn(0.9)
    rec = recorder("SSSSSS" + "." * 40, turn, end_silence_s=0.8, max_pause_s=1.6)
    rec.record(mic())
    assert turn.asked == 1 and rec.trailing_silence_s == pytest.approx(0.8)


def test_someone_who_sounds_mid_thought_gets_a_longer_pause():
    turn = FakeTurn(0.1)
    rec = recorder("SSSSSS" + "." * 40, turn, end_silence_s=0.8, max_pause_s=1.6)
    rec.record(mic())
    assert turn.asked == 1 and rec.trailing_silence_s == pytest.approx(1.6)


def test_talking_again_after_a_long_pause_keeps_the_whole_sentence():
    """ "Tell me a fun fact about..." (thinks for 1.2 s) "...octopuses." """
    turn = FakeTurn(0.1)
    pcm = recorder("SSSSSS" + "." * 15 + "SSSS" + "." * 40, turn, end_silence_s=0.8, max_pause_s=1.6).record(mic())
    assert blocks_in(pcm) >= 6 + 15 + 4


def test_without_the_model_the_pause_is_just_the_silence_wait():
    rec = recorder("SSSSSS" + "." * 40, end_silence_s=0.8)
    rec.record(mic())
    assert rec.trailing_silence_s == pytest.approx(0.8)


def test_a_pause_is_announced_early_and_talking_again_after_it_too():
    events = []
    rec = recorder("SSSSSS" + "..." + "SSSS" + "." * 12, end_silence_s=0.8, answer_early_s=0.2)
    rec.record(mic(), on_pause=lambda pcm: events.append("pause"), on_resume=lambda: events.append("resume"))
    assert events == ["pause", "resume", "pause"]


def test_soft_words_after_the_early_answer_started_throw_it_away():
    events = []
    rec = recorder("SSSSSS" + "...." + "mmm" + "." * 12, end_silence_s=0.8, answer_early_s=0.2)
    pcm = rec.record(mic(), on_pause=lambda pcm: events.append("pause"), on_resume=lambda: events.append("resume"))
    assert events[:2] == ["pause", "resume"]
    assert blocks_in(pcm) >= 6 + 4 + 3  # the soft words are kept


@pytest.mark.skipif(not VAD_MODEL.exists(), reason="speech detector not downloaded yet")
def test_the_speech_detector_ignores_our_chime_and_quiet():
    vad = SileroVAD(VAD_MODEL)
    assert max(vad(chime_block(f)) for f in (880.0, 1320.0) for _ in range(4)) < 0.3
    assert max(vad(quiet_block()) for _ in range(20)) < 0.3


def test_dead_microphone_raises_instead_of_hanging(monkeypatch):
    import voice_assistant.audio as audio

    monkeypatch.setattr(audio, "MIC_STALL_S", 0.1)
    mic = Microphone.__new__(Microphone)  # skip opening a real device
    import queue

    mic._queue = queue.Queue()
    with pytest.raises(MicrophoneError):
        mic.read()


def test_sentences_are_split_as_they_stream_in():
    pieces = ["Hello th", "ere. It is 3.5 deg", "rees! Want more?", " Ok"]
    assert list(split_sentences(pieces)) == ["Hello there.", "It is 3.5 degrees!", "Want more?", "Ok"]


def test_markdown_is_stripped_before_speaking():
    assert clean_for_speech("**Bold** and `code` # heading ") == "Bold and code  heading"


def test_speaker_box_is_identical_whether_streamed_or_whole():
    audio = (np.random.default_rng(1).normal(0, 3000, 24_000 * 2)).astype(np.int16).tobytes()
    whole = SpeakerBox(24_000).process(audio)
    box = SpeakerBox(24_000)
    streamed = b"".join(box.process(audio[i : i + 4096]) for i in range(0, len(audio), 4096))
    assert streamed == whole


def test_voice_with_effect_handles_odd_sized_chunks():
    class OddVoice:
        sample_rate = 24_000

        def stream(self, text):
            yield b"\x01"  # half a sample
            yield b"\x00" * 999

    out = b"".join(VoiceWithEffect(OddVoice(), "tars").stream("hi"))
    assert len(out) % 2 == 0 and len(out) >= 1000


def test_no_effect_returns_the_voice_unchanged():
    voice = object()
    assert apply_effect(voice, "") is voice


def test_unknown_effect_is_a_clear_error():
    with pytest.raises(SystemExit, match="tars"):
        VoiceWithEffect(object(), "robot")
    assert "tars" in EFFECTS


def test_repo_config_loads_with_every_section(tmp_path):
    cfg = load_config(__import__("pathlib").Path(__file__).parents[1] / "config.toml")
    assert isinstance(cfg, Config)
    assert cfg.tts.effect == "tars" and cfg.recorder.follow_up_s > 0 and cfg.llm.memory_minutes > 0


def test_missing_config_falls_back_to_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.speaker.enabled is False and cfg.tts.effect == ""


@pytest.mark.parametrize(
    "setting",
    [
        '[stt]\nprovider = "whisper"',
        "[recorder]\nmax_pause_s = 0.5",
        "[recorder]\nanswer_early_s = 0.9",
        "[recorder]\nvad_threshold = 0.1",
    ],
)
def test_a_setting_that_cannot_work_stops_at_startup(tmp_path, setting):
    path = tmp_path / "config.toml"
    path.write_text(setting)
    with pytest.raises(SystemExit):
        load_config(path)
