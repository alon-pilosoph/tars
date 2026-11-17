import httpx
import numpy as np
import soundfile as sf

from voice_assistant import extract
from voice_assistant.extract import Extractor, SecondChance, kept_enough, mixed, pack, unpack
from voice_assistant.verify import ANSWER, IGNORE

from .conftest import FakeTrigger, SilentMic
from .test_verify import listening

RATE = 16000


def talk(seconds: float, level: float, seed: int = 0) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * RATE)) * level).astype(np.int16)


def window(background: float, name: float) -> np.ndarray:
    """3 s ending in "hey TARS" (the last NAME_S) at level `name`, after 1.8 s at level `background`."""
    lead = 3.0 - extract.NAME_S
    before = talk(lead, background) if background else np.zeros(int(lead * RATE), np.int16)
    return np.concatenate([before, talk(extract.NAME_S, name, 1)])


def loud_is_speech(step: np.ndarray) -> float:
    return float(np.abs(step).mean() > 50)


def test_a_wake_over_other_speech_is_mixed_and_one_in_quiet_isnt():
    assert mixed(window(2000, 3000), loud_is_speech, 0.3, 12.0)
    assert not mixed(window(0, 3000), loud_is_speech, 0.3, 12.0)
    # Something talking far under the name doesn't count: the TV turned low.
    assert not mixed(window(200, 6000), loud_is_speech, 0.3, 12.0)


def test_an_extraction_that_kept_almost_nothing_isnt_trusted():
    audio = talk(1.0, 3000)
    assert kept_enough(audio.astype(np.float32) / 32768 * 0.5, audio, 0.05)
    assert not kept_enough(np.zeros(RATE, np.float32), audio, 0.05)
    assert not kept_enough(np.full(RATE, np.nan, np.float32), audio, 0.05)


def enrolled(tmp_path, *names: str):
    for i, name in enumerate(names):
        (tmp_path / name).mkdir()
        sf.write(str(tmp_path / name / "a.wav"), talk(2.0, 2000, 10 + i), RATE, subtype="PCM_16")
    return tmp_path


def server(reply):
    """An httpx client whose server answers each request with `reply(mix, enroll)`, and the requests it got."""
    got = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent = unpack(request.content)
        got.append(sent)
        return httpx.Response(200, content=pack(voice=reply(sent["mix"], sent["enroll"])))

    return httpx.Client(transport=httpx.MockTransport(handle)), got


def test_each_enrolled_voice_is_sent_with_the_window_and_comes_back_as_audio(tmp_path):
    http, got = server(lambda mix, enroll: mix.astype(np.float32) / 32768 * 0.5)
    ex = Extractor("http://x/extract", enrolled(tmp_path, "alon", "stacey"), 1.0, 0.05, http)
    audio = talk(3.0, 3000)
    voices = list(ex.each_voice(audio))
    assert list(ex.voices) == ["alon", "stacey"] and len(voices) == 2 and len(got) == 2
    assert voices[0].dtype == np.int16 and np.array_equal(got[0]["mix"], audio)


def test_a_failed_server_gives_no_second_chance(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    ex = Extractor("http://x/extract", enrolled(tmp_path, "alon"), 1.0, 0.05, http)
    assert list(ex.each_voice(talk(3.0, 3000))) == []


class Detector:
    def reset(self):
        pass

    def __call__(self, step):
        return loud_is_speech(step)


def test_a_wake_in_quiet_is_never_sent_anywhere(tmp_path):
    http, got = server(lambda mix, enroll: mix.astype(np.float32) / 32768)
    ex = Extractor("http://x/extract", enrolled(tmp_path, "alon"), 1.0, 0.05, http)
    chance = SecondChance(ex, Detector(), 0.3, 12.0)
    assert list(chance(window(0, 3000))) == [] and got == []
    assert len(list(chance(window(2000, 3000)))) == 1 and len(got) == 1


def test_a_wake_turned_down_raw_is_answered_when_the_extracted_voice_passes(hearing):
    trigger = FakeTrigger(fire_at=[3])
    verifier = hearing("")
    clean = np.full(100, 7, np.int16)
    verifier.check = lambda pcm: (True, "hey tars") if np.array_equal(pcm, clean) else (False, "the news at")
    t = listening(trigger, verifier)
    t._second_chance = lambda audio: iter([clean])
    assert t.wait(SilentMic(10)) == ANSWER


def test_the_second_chance_never_turns_an_answer_down_or_asks(hearing):
    trigger = FakeTrigger(fire_at=[3])
    verifier = hearing("hey tars")
    t = listening(trigger, verifier)
    t._second_chance = lambda audio: (_ for _ in ()).throw(AssertionError("not asked when the raw check passes"))
    assert t.wait(SilentMic(10)) == ANSWER
    # A failing second chance leaves the raw answer: turned down, still listening.
    trigger, verifier = FakeTrigger(fire_at=[3, 6]), hearing("")
    answers = iter([(False, "x"), (True, "hey tars")])
    verifier.check = lambda pcm: next(answers)
    t = listening(trigger, verifier)
    t._second_chance = lambda audio: (_ for _ in ()).throw(RuntimeError("server gone"))
    assert t.wait(SilentMic(10)) == ANSWER and trigger.n == 6
    assert IGNORE == "ignore"


class Pieces:
    """An extractor that hands back, for each piece, the voice part of the clip it was cut from: `voice`, at the same
    place in time (an int16 track as long as the whole recording; `end`, where the piece ends, set by `feed`)."""

    def __init__(self, voice: np.ndarray, fail_after: int | None = None):
        self.voice, self.calls, self.fail_after, self.end = voice, [], fail_after, 0

    def extract(self, audio, reference, whole=True):
        self.calls.append(len(audio))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            return None
        return self.voice[self.end - len(audio) : self.end].copy()


def feed(focus, mix: np.ndarray, extractor: Pieces) -> list[float]:
    probs = []
    for i in range(0, len(mix), 1280):
        extractor.end = i + 1280
        probs.append(focus(mix[i : i + 1280]))
    return probs


def test_a_focused_recording_hears_only_their_voice_and_keeps_it_without_the_tv():
    from voice_assistant.extract import FocusedSpeech

    n = RATE * 4
    voice = np.zeros(n, np.int16)
    voice[: RATE * 2] = talk(2.0, 3000)  # they talk for 2 s, the TV goes on
    mix = (voice.astype(np.int32) + talk(4.0, 2500, 5)).clip(-32768, 32767).astype(np.int16)
    extractor = Pieces(voice)
    focus = FocusedSpeech(extractor, talk(1.0, 2000), Detector())
    probs = feed(focus, mix, extractor)
    assert max(probs[:20]) == 1.0 and max(probs[-10:]) == 0.0  # the TV alone isn't speech
    assert all(c <= int(FocusedSpeech.CONTEXT_S * RATE) for c in extractor.calls)
    kept = focus.pcm()
    assert kept is not None and np.abs(kept[RATE * 2 + 8000 :]).max() == 0  # the TV isn't in what's transcribed


def test_a_focused_recording_whose_server_fails_hears_the_raw_audio_from_then_on():
    from voice_assistant.extract import FocusedSpeech

    mix = talk(3.0, 2500, 5)
    extractor = Pieces(np.zeros(len(mix), np.int16), fail_after=1)
    focus = FocusedSpeech(extractor, talk(1.0, 2000), Detector())
    probs = feed(focus, mix, extractor)
    assert focus.failed and probs[-1] == 1.0 and focus.pcm() is None


def embedder(reference: np.ndarray):
    """A stand-in speaker embedding: audio that is `reference`'s voice (it follows it, sample for sample) comes back
    as one direction, anything else as another."""

    def embed(voice):
        n = min(len(voice), len(reference), 4000)
        same = np.corrcoef(voice[:n].astype(float), reference[:n].astype(float))[0, 1] > 0.5
        return np.array([1.0, 0.0]) if same else np.array([0.0, 1.0])

    return embed


def test_focus_is_only_for_an_enrolled_voice_whose_wake_was_said_over_other_speech(tmp_path):
    from voice_assistant.extract import FocusedSpeech, RequestFocus

    http, _ = server(lambda mix, enroll: enroll[: len(mix)].astype(np.float32) / 32768)
    ex = Extractor("http://x/extract", enrolled(tmp_path, "alon"), 1.0, 0.05, http)
    rf = RequestFocus(ex, Detector(), lambda pcm: "text", 0.3, 12.0, embedder(ex.voices["alon"]), 0.4)
    assert rf.for_wake(None) is None
    assert rf.for_wake(window(0, 3000)) is None  # said in quiet
    assert isinstance(rf.for_wake(window(2000, 3000)), FocusedSpeech)


def test_a_wake_whose_extraction_sounds_like_nobody_enrolled_isnt_focused(tmp_path):
    from voice_assistant.extract import RequestFocus

    # A guest: whatever the reference, what comes back is their voice, not the reference's owner's.
    http, _ = server(lambda mix, enroll: mix.astype(np.float32) / 32768)
    ex = Extractor("http://x/extract", enrolled(tmp_path, "alon"), 1.0, 0.0, http)
    rf = RequestFocus(ex, Detector(), lambda pcm: "text", 0.3, 12.0, embedder(ex.voices["alon"]), 0.4)
    assert rf.for_wake(window(2000, 3000)) is None


class LocalFakeBody:
    """FakeBody's recording, with a recorder of its own (as a local body has): what each recording was given."""

    recorder = None


def test_a_conversation_woken_over_the_tv_hears_one_voice(monkeypatch):
    from voice_assistant.body import Wake

    from .conftest import FakeBody, make_assistant, speech

    class Body(FakeBody, LocalFakeBody):
        def record(self, **kw):
            self.speech = kw.get("speech")
            return super().record(**kw)

    class Focus:
        def reset(self):
            pass

    class Maker:
        def for_wake(self, audio):
            return Focus()

        def transcribe(self, focus, pcm):
            return "turn on the lights"

    assistant, brain = make_assistant([speech()], [])
    assistant.body = Body([speech()])
    assistant._focus_maker = Maker()
    assistant.converse(0.0, wake=Wake(ANSWER, talk(3.0, 2000)), greet="never")
    assert isinstance(assistant.body.speech, Focus)
    assert brain.asked and "turn on the lights" in brain.asked[0] and "[Said over" not in brain.asked[0]
    assert assistant._focus is None  # only for that conversation
