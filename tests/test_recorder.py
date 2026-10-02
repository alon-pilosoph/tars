"""The recorder: when an utterance starts and ends, by its own silence rules or by a service that follows the turn."""

from pathlib import Path

import pytest

from voice_assistant.audio import BLOCK_SAMPLES
from voice_assistant.config import RecorderConfig
from voice_assistant.enroll import PAD_BLOCKS
from voice_assistant.recorder import UtteranceRecorder
from voice_assistant.stt import TurnState
from voice_assistant.vad import SileroVAD

from .conftest import FakeMic, quiet_block

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


class CountingMic(FakeMic):
    def __init__(self, n=200):
        super().__init__([quiet_block() for _ in range(n)])
        self.reads = 0

    def read(self):
        self.reads += 1
        return super().read()


def mic(n=200):
    return CountingMic(n)


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


def test_someone_who_never_stops_is_cut_off_at_the_longest_utterance():
    rec = recorder("S" * 300, max_utterance_s=2.0)
    pcm = rec.record(mic(300))
    assert blocks_in(pcm) == 25 + 2  # 2 s of 80 ms blocks, preroll included, and the usual tail
    assert rec.trailing_silence_s == 0.0


class FakeTurn:
    def __init__(self, finished):
        self.p, self.asked = finished, 0

    def p_finished(self, pcm):
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
def test_the_speech_detector_ignores_quiet():
    vad = SileroVAD(VAD_MODEL)
    assert max(vad(quiet_block()) for _ in range(20)) < 0.3


class FluxTurns:
    """Stands in for a turn-taking service: its state, block by block, from a pattern: L listening, M maybe done,
    D done."""

    def __init__(self, pattern):
        states = {"L": TurnState.LISTENING, "M": TurnState.MAYBE_DONE, "D": TurnState.DONE}
        self.states = iter(states[c] for c in pattern)
        self.last = TurnState.LISTENING

    def __call__(self):
        self.last = next(self.states, self.last)
        return self.last


def test_with_flux_its_end_of_turn_ends_the_recording():
    rec = recorder("SSSSSS" + "." * 40, FakeTurn(0.1), end_silence_s=0.8, max_pause_s=1.6)
    # Asked from the 3rd speech block on (2 start the recording): "done" arrives on the 5th quiet block.
    rec.record(mic(), turn_state=FluxTurns("L" * 8 + "D"))
    assert rec.trailing_silence_s == pytest.approx(0.40)  # neither the silence wait nor Smart Turn's extension


def test_once_flux_calls_the_turn_no_more_audio_is_waited_for():
    rec = recorder("SSSSSS" + "." * 40)
    m = mic()
    pcm = rec.record(m, turn_state=FluxTurns("L" * 3 + "D"))  # done as the last word ends
    assert m.reads == 6 and blocks_in(pcm) == 6  # no tail read after it


def test_with_flux_its_maybe_starts_a_draft_and_taking_it_back_throws_it_away():
    events = []
    rec = recorder("SSSSSS" + "." * 40, end_silence_s=0.8, answer_early_s=0.2)
    rec.record(
        mic(),
        on_pause=lambda pcm: events.append("pause"),
        on_resume=lambda: events.append("resume"),
        turn_state=FluxTurns("L" * 7 + "MM" + "LL" + "MM" + "D"),
    )
    assert events == ["pause", "resume", "pause"]  # the silence itself announced nothing


def test_a_breath_during_flux_s_maybe_does_not_throw_the_draft_away():
    events = []
    rec = recorder("SSSSSS" + "..." + "mm" + "." * 20, answer_early_s=0.2)
    rec.record(
        mic(),
        on_pause=lambda pcm: events.append("pause"),
        on_resume=lambda: events.append("resume"),
        turn_state=FluxTurns("L" * 7 + "M" * 4 + "D"),
    )
    assert events == ["pause"]  # Flux kept its maybe: only Flux takes it back


def test_if_flux_goes_quiet_the_backstop_ends_the_turn():
    rec = recorder("SSSSSS" + "." * 60, end_silence_s=0.8)
    rec.record(mic(), turn_state=FluxTurns("L"))
    assert rec.trailing_silence_s == pytest.approx(2.56)  # BACKSTOP_S, in whole 80 ms blocks


def test_if_flux_fails_mid_turn_the_recorder_s_own_rules_take_over():
    events = []
    rec = recorder("SSSSSS" + "." * 60, end_silence_s=0.8, answer_early_s=0.2)
    rec.record(mic(), on_pause=lambda pcm: events.append("pause"), turn_state=lambda: TurnState.FAILED)
    assert rec.trailing_silence_s == pytest.approx(0.8) and events == ["pause"]  # not the 2.5 s backstop
