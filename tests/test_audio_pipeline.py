"""Recorder, speech splitting, the TARS effect and config: the parts that don't need a device or an API."""

import numpy as np
import pytest

from voice_assistant.audio import BLOCK_SAMPLES, Microphone, MicrophoneError
from voice_assistant.config import Config, RecorderConfig, load_config
from voice_assistant.effects import EFFECTS, SpeakerBox, VoiceWithEffect, apply_effect
from voice_assistant.enroll import PAD_BLOCKS
from voice_assistant.recorder import UtteranceRecorder
from voice_assistant.speech import clean_for_speech, split_sentences

from .conftest import FakeMic, chime_block, quiet_block, voiced_block

SPEECH_BLOCKS = 12
UTTERANCE = (
    [quiet_block() for _ in range(20)]
    + [voiced_block() for _ in range(SPEECH_BLOCKS)]
    + [quiet_block() for _ in range(40)]
)


def blocks_in(pcm: bytes) -> int:
    return len(pcm) // 2 // BLOCK_SAMPLES


def test_recorder_trims_to_the_speech():
    pcm = UtteranceRecorder(RecorderConfig()).record(FakeMic(UTTERANCE))
    assert SPEECH_BLOCKS <= blocks_in(pcm) <= SPEECH_BLOCKS + 8


def test_recorder_keeps_padding_when_asked():
    default = UtteranceRecorder(RecorderConfig()).record(FakeMic(UTTERANCE))
    padded = UtteranceRecorder(RecorderConfig()).record(
        FakeMic(UTTERANCE), preroll_blocks=PAD_BLOCKS, tail_blocks=PAD_BLOCKS
    )
    assert blocks_in(padded) - blocks_in(default) >= 2 * PAD_BLOCKS - 8  # about a second on each side


def test_our_own_chime_does_not_count_as_speech():
    """The chime leaks back into the mic; it used to start (and end) a recording before anyone spoke."""
    rec = UtteranceRecorder(RecorderConfig())
    assert not rec.is_speech(chime_block(880.0))
    assert not rec.is_speech(chime_block(1320.0))  # the follow-up blip
    assert rec.is_speech(voiced_block())
    # A fresh recorder: webrtcvad keeps a short "still talking" state after the voice block above.
    chime_then_silence = [chime_block() for _ in range(3)] + [quiet_block() for _ in range(60)]
    assert UtteranceRecorder(RecorderConfig()).record(FakeMic(chime_then_silence), start_timeout_s=4.0) is None


def test_a_lone_speech_block_in_the_silence_does_not_keep_the_recording_going(monkeypatch):
    """webrtcvad flags a block of steady room noise as speech now and then; each used to restart the wait."""
    rec = UtteranceRecorder(RecorderConfig(end_silence_s=0.4))  # 5 blocks
    heard = iter("SSSSSS" + "...S...S...S" + "." * 20)
    monkeypatch.setattr(rec, "is_speech", lambda block: next(heard) == "S")
    pcm = rec.record(FakeMic([quiet_block() for _ in range(40)]))
    assert blocks_in(pcm) <= 6 + 12 - 4  # it stopped within the stray blocks, not after them


def test_talking_again_after_a_pause_keeps_recording(monkeypatch):
    rec = UtteranceRecorder(RecorderConfig(end_silence_s=0.4))
    heard = iter("SSSS" + "..." + "S.SS.S" + "." * 20)  # speech flickers
    monkeypatch.setattr(rec, "is_speech", lambda block: next(heard) == "S")
    pcm = rec.record(FakeMic([quiet_block() for _ in range(40)]))
    assert blocks_in(pcm) >= 11


def test_recorder_gives_up_when_nobody_speaks():
    silence = [quiet_block() for _ in range(100)]
    assert UtteranceRecorder(RecorderConfig()).record(FakeMic(silence), start_timeout_s=1.0) is None


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
