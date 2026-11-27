"""A phone call with TARS: one conversation that stays awake until someone hangs up (docs/phone.md).

The mind and the voice are the ones TARS has everywhere: the call's brain, its tools and its rules, and the same voice.
What's different is how turns are taken. Flux listens to the whole call on one stream and says when the caller starts,
may be done, carries on, and is done (stt.FluxCall). A reply is started at "may be done" and played once they are, so
it's often ready; what it does in the house still waits for the turn to be over (Draft.may_act).

TARS can be talked over. Words that could be the caller taking the turn pause it at once; two words, or one that's
clear, and the rest is dropped, keeping only what was heard, to the word. If nothing comes of them (a word from the
radio), it goes on from where it paused. "Mm-hm", and TARS's own words back from a car's speakers, don't pause it.

Something slow (a search) goes on if they talk meanwhile: it's carried over to a brain of its own, while a new one
with the same conversation takes what they say, and what it found is said in the next quiet moment.

A call is never at home: requests are a web page's (home.Request.by_web), so nothing said on one unlocks a door.

One thread, the loop's, changes the state of the call; the others (Flux's, a reply's) only tell it things.
"""

import queue
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import usage
from .approvals import Approvals, for_other
from .assistant import NOBODY, PHRASE_WAIT_S, Answer, Assistant, Identified, offered
from .audio import silent_bytes
from .body import BodyGone
from .bodylink import BodyConnection
from .closing import DONE, closing
from .conversations import NOTE, TTS, SentItem
from .draft import Draft
from .errors import ServiceError
from .home import ONE_MOMENT, Request
from .llm import Brain, Reply, only_filler
from .signin import shown
from .speech import failed_at, mark_failed_at, split_sentences
from .stt import FluxCall

PHONE_RULES = (
    "You're on a phone call, and they may be driving. Talk the way people do on the phone: one or two short "
    "sentences, then let them speak. No lists, nothing to look at; send anything long or visual and say you sent it. "
    "Every message on the call is meant for you. Ask before anything hard to undo.\n"
    "Some things aren't done on a call alone (changing TARS or Home Assistant, automations, a message or reminder for "
    "someone else): they go to the caller's Telegram chat, and are done once they tap Approve. Then say it's waiting "
    "for their tap, never that it's done or sent.\n"
    "[Interrupted you] means they spoke over you: they heard only what your last message says, so don't assume the "
    "rest, and don't start it again unless they want it. [Interrupted you before you answered] means they heard "
    "nothing: whatever you meant to send or set was not kept, and a device may or may not have changed, so check "
    "before saying it did."
)
INTERRUPTED, UNHEARD = "[Interrupted you]", "[Interrupted you before you answered]"
CUT = "…"  # where a reply was cut off, in the conversation
GOODBYE = "Talk later."
LOST = "I can't hear you properly. Call me back in a minute."
STILL_ON_IT = "Still on it."
FOUND = "About what you asked before."
TICK_S = 0.03
QUIET_S = 120.0  # nobody has said anything for this long: the call is over
MAX_CALL_S = 1800.0
SENT_AHEAD_S = 0.25  # from sending a sound to the caller hearing it
MARK_S = 0.4  # how often the phone says how far it has played
LOST_MARK_S = 5.0  # a reply whose last mark hasn't come back by this long after its end has played
RESUME_S = 2.0  # paused, and nothing more was said: it goes on
SURE = 0.6  # how sure Flux must be of a word that takes the turn alone
EARLY_TRIES, EARLY_MAX_S = 3, 10.0  # answers started early in one turn, and how far into it
SLOW_S = 2.5  # a reply with no sound by then: "One moment."
STILL_S = 9.0  # and this long after the last sound: "Still on it."
MAX_FILLS = 3
DETACH_S = 1.5  # a reply this long in the making is worth finishing when they talk over it
MAX_LATER = 2
QUIET_GAP_S = 0.8  # nobody has spoken for this long: what was found meanwhile can be said
LATER_WAIT_S = 60.0  # after they hung up, how long what's still being found may take to be sent instead
NEXT_SENTENCE_S = 0.3  # a sentence this late starts the voice afresh rather than holding it open
SETTLE_S = 5.0
FLUX_TRIES = 3  # streams lost in a row before TARS gives up on the call

# Said while TARS talks, these mean "go on".
GO_ON = re.compile(r"(?:mm+|mm ?hm+|m+hm+|uh ?huh|hm+|uh|um|yeah|yep|yes|ok|okay|right|sure|cool|nice|great|i see|"
                   r"got it|wow)(?: (?:mm+|mm ?hm+|uh ?huh|hm+|yeah|yep|ok|okay|right|sure))*")  # fmt: skip
STOPS = frozenset(["stop", "wait", "no", "hold", "tars", "actually", "enough", "quiet", "pause", "sorry"])
CANCELS = frozenset(["stop", "wait", "no", "cancel", "never", "forget", "actually", "don't"])
BYE = re.compile(r"\b(?:bye|goodbye|see you|that'?s (?:all|it)|we'?re done|i'?m done)\b")
HANG_UP = re.compile(r"(?:(?:ok|okay|please|you can|tars) )*hang up(?: (?:now|please|tars|the phone))*")


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s']", " ", text.lower().replace("’", "'")).split()


def says_bye(text: str) -> bool:
    said = " ".join(words(text))
    return bool(HANG_UP.fullmatch(said)) or (closing(text) == DONE and bool(BYE.search(said)))


def goes_on(text: str) -> bool:
    """Nothing but "mm-hm": they're listening, not taking the turn."""
    return bool(GO_ON.fullmatch(" ".join(words(text))))


def echoes(text: str, spoken: str) -> bool:
    """TARS's own words, come back: `text` is a run of what it's saying."""
    heard, said = words(text), words(spoken)
    n = len(heard)
    return 0 < n <= len(said) and any(said[i : i + n] == heard for i in range(len(said) - n + 1))


def takes_the_turn(text: str, spoken: str) -> bool:
    """Whether what's been heard so far, while TARS says `spoken`, is the caller taking the turn."""
    if not words(text):
        return False
    if spoken.rstrip().endswith("?"):  # TARS asked: "yeah", or the choice it named, is the answer
        return not (len(words(text)) >= 4 and echoes(text, spoken))
    return not goes_on(text) and not echoes(text, spoken)


class _NobodyToAsk(Approvals):
    """With no chat to ask in, what needs a tap is refused."""

    def __init__(self):
        self._chat_of = lambda person: None


NOBODY_TO_ASK = _NobodyToAsk()


@dataclass
class _Turn:
    """What TARS is saying, or about to: a reply, or (no draft) a line of its own."""

    text: str = ""  # what it answers
    prompt: str = ""
    draft: Draft | None = None
    answer: Answer | None = None
    brain: Brain | None = None
    went: bool = False  # the caller is done: it may be heard
    cut: bool = False
    later: bool = False  # being finished while the call goes on
    ends_call: bool = False
    stop: threading.Event = field(default_factory=threading.Event)  # to the thread making it
    # What's made of it, in the order it's heard. Added to by the loop alone, as the thread making it says.
    audio: bytearray = field(default_factory=bytearray)
    said: list[str] = field(default_factory=list)  # its sentences
    words: list[tuple[str, float]] = field(default_factory=list)  # (word, when it ends), where it's known
    started: bool = False  # its own first sound is in
    made: bool = False  # nothing more is coming
    real: bool = False  # something in it isn't "One moment"
    # What's been sent of it, and heard.
    sent: int = 0
    playing: bool = False
    marks: dict[int, int] = field(default_factory=dict)  # mark: how far in (bytes) it was put
    last_mark: int = 0  # how far in the newest is
    anchor: tuple[int, float] = (0, 0.0)  # heard up to here, at this time
    paused_at: float | None = None
    heard: str = ""  # once cut
    fills: int = 0
    shifted: float = 0.0  # seconds the reply's own timeline is moved by, by fills put in and silence taken off
    ended_at: float = 0.0  # when the caller was done, by the loop's clock and perf_counter
    ended_pc: float = 0.0
    waited: float | None = None  # from their last word to Flux being sure they were done
    first_sound: float | None = None

    @property
    def spoken(self) -> str:
        return " ".join(self.said)


class CallConversation:
    """One call. `assistant`: the call's own (its brain, voice, journal and reminders). `link`: the phone's body link.
    `listen(event)`: opens a FluxCall whose events go to `event`. `new_brain()`: another brain like the call's, for
    carrying on while the first is busy; None: something slow is dropped when it's talked over. `approvals`: what asks
    for a tap before anything that needs one; None: those are refused."""

    def __init__(
        self,
        assistant: Assistant,
        link: BodyConnection,
        listen: Callable[[Callable[[str, str, float], None]], FluxCall],
        new_brain: Callable[[], Brain] | None = None,
        approvals: Approvals | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.a, self.link, self._listen, self._new_brain, self._clock = assistant, link, listen, new_brain, clock
        self.person, self._approvals = assistant.person, approvals
        self._rate = assistant.voice.sample_rate
        self._events: queue.Queue[tuple] = queue.Queue()
        self._thinking = threading.Lock()  # a brain's drafts take turns
        self._ids = iter(range(1, 2**31))
        self._now: _Turn | None = None  # being said, or about to be
        self._draft: _Turn | None = None  # started at "may be done", the caller not done yet
        self._later: list[_Turn] = []
        self._tag = ""
        self._talking = False  # the caller is in a turn
        self._ignoring = False  # ...that isn't taking the turn (yet)
        self._turn_began = 0.0
        self._tries = 0
        self._last_spoken = ""
        self._flux: FluxCall | None = None
        self._ears: list = []
        self._lost = 0
        self._over = threading.Event()
        self._began = self._last = clock()
        self.totals: list[float] = []  # from the caller's last word to TARS's first sound leaving here
        self.counts = {"interrupted": 0, "resumed": 0, "ignored": 0, "early": 0, "later": 0}

    # The call

    def hold(self) -> None:
        """The call, until either side hangs up."""
        with usage.turn(self.person, usage.VOICE):
            try:
                self._start()
                while not self.link.gone.is_set() and not self._over.is_set():
                    self._step()
            except BodyGone:
                pass  # they hung up
            finally:
                self._finish()

    def _start(self) -> None:
        self._began = self._last = self._clock()
        self._open_ears()
        threading.Thread(target=self._pump, daemon=True, name="call-audio").start()
        threading.Thread(target=self.a.brain.warm, daemon=True, name="brain-warm").start()
        self.a.voice.warm()
        hello = f"Hey {shown(self.person)}." if self.person else "Hey."
        self._say(hello)
        if not usage.capped():
            self.a.prepare_phrases()
            for line in (GOODBYE, LOST, STILL_ON_IT, FOUND):
                self.a.phrases.get(line, ahead=True)
        self.a.journal.said(hello)
        self.a.brain.remember_said(hello)

    def _finish(self) -> None:
        self._over.set()
        if self._flux:
            self._flux.close()
        if self._now:
            self._later_if_slow(self._now, "")
        self._interrupt()
        self._drop_draft()
        if self._thinking.acquire(timeout=SETTLE_S):  # what was cut is kept before the conversation ends
            self._thinking.release()
        self._send_what_was_found()
        self.a.journal.end_conversation()
        self._report()

    def _report(self) -> None:
        took = sorted(self.totals)
        if took:
            p50, p95 = took[len(took) // 2], took[min(len(took) - 1, int(len(took) * 0.95))]
            print(f"(call: {len(took)} answers; first sound {p50:.2f}s typical, {p95:.2f}s slow end)", flush=True)
        print(f"(call: {', '.join(f'{v} {k}' for k, v in self.counts.items())})", flush=True)

    def _step(self) -> None:
        try:
            event = self._events.get(timeout=TICK_S)
            while True:
                self._take(*event)
                event = self._events.get_nowait()
        except queue.Empty:
            pass
        while not self.link.buttons.empty():
            self.link.buttons.get_nowait()
            self._interrupt()
        self._note_played()
        self._resume_if_nothing_came()
        self._fill()
        self._say_what_was_found()
        self._send_ready()
        now = self._clock()
        if now - self._began > MAX_CALL_S or (now - self._last > QUIET_S and self._now is None):
            self._goodbye()

    def _take(self, kind: str, *what) -> None:
        if kind == "flux":
            if not self._over.is_set():
                self._heard(*what)
        elif kind == "sound":
            turn, sound = what
            if not turn.started:  # a voice starts with silence: TARS starts with its first sound
                quiet = silent_bytes(sound)
                turn.shifted -= quiet / 2 / self._rate
                sound = sound[quiet:]
                turn.started = bool(sound)
            turn.audio += sound
        elif kind == "sentence":
            turn, sentence = what
            turn.said.append(sentence)
            turn.real = turn.real or not only_filler(sentence)
            print(f"Bot (call):  {sentence}", flush=True)
        elif kind == "words":
            turn, words_at = what
            turn.words += [(w, end + turn.shifted) for w, end in words_at]
        elif kind == "made":
            what[0].made = True
        elif kind == "failed":
            self._failed(*what)

    def _pump(self) -> None:
        while not self._over.is_set() and not self.link.gone.is_set():
            try:
                block = self.link.blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            if flux := self._flux:
                flux.feed(block.tobytes())

    def _open_ears(self) -> None:
        ears: list = []
        ears.append(self._listen(lambda kind, text, sure=1.0: self._events.put(("flux", kind, text, sure, ears))))
        self._flux, self._ears = ears[0], ears

    # What's heard

    def _heard(self, kind: str, text: str, sure: float, ears: list) -> None:
        if ears is not self._ears:  # a stream already given up on
            return
        if kind == "Failed":
            self._lost_ears(text)
            return
        self._lost = 0
        self._last = self._clock()
        if kind == "StartOfTurn":
            self._talking, self._ignoring, self._turn_began, self._tries = True, False, self._last, 0
        self._judge(kind, text, sure)
        if kind == "TurnResumed":
            self._drop_draft()
        elif kind == "EagerEndOfTurn" and self._now is None and not self._ignoring:
            early = self._tries < EARLY_TRIES and self._last - self._turn_began < EARLY_MAX_S
            if early:
                self._prepare(text)
        elif kind == "EndOfTurn":
            self._talking = False
            if self._ignoring:
                self.counts["ignored"] += 1
                self._drop_draft()
                print(f"(call: not taking the turn: {text!r})", flush=True)
            else:
                self._go(text)
            self._ignoring = False

    def _judge(self, kind: str, text: str, sure: float) -> None:
        """Whether what they're saying takes the turn from what TARS is saying, or about to."""
        turn = self._now
        if turn is None:
            if self._ignoring:  # TARS finished while they went on: more than "mm-hm" is theirs to say now
                self._ignoring = not takes_the_turn(text, self._last_spoken)
            return
        said = words(text)
        if not turn.playing and turn.paused_at is None:  # still thinking
            taken = bool(said) and not goes_on(text)
        else:
            taken = takes_the_turn(text, turn.spoken)
        if not taken:
            self._ignoring = True
            if kind == "EndOfTurn":
                self._resume(turn)
            return
        self._ignoring = False
        clear = len(said) >= 2 or said[0] in STOPS or (kind == "EndOfTurn" and sure >= SURE)
        if clear:
            self._later_if_slow(turn, text)
            self._interrupt()
        elif kind == "EndOfTurn":  # one unsure word, and nothing came of it
            self._ignoring = True
            self._resume(turn)
        else:
            self._pause(turn)

    def _lost_ears(self, why: str) -> None:
        self._lost += 1
        self._talking = False
        self._drop_draft()
        print(f"(call: the stream to Flux ended: {why})", flush=True)
        if self._now:
            self._resume(self._now)
        if self._lost >= FLUX_TRIES:
            self._interrupt()
            self._say(LOST, ends_call=True)
            self._ears = []
        else:
            self._flux.close()
            self._open_ears()

    # Answering

    def _prepare(self, text: str) -> None:
        """Starts answering `text`, in case they're done."""
        if self._draft and self._draft.text == text:
            return
        self._drop_draft()
        if not words(text) or says_bye(text) or usage.capped():
            return
        self._tries += 1
        turn = _Turn(text, self._prompt(text), brain=self.a.brain)
        turn.draft = Draft(
            self._thinking, lambda draft: self._write(draft, turn), lambda answer: self._throw_away(turn, answer)
        )
        self._draft = turn

    def _prompt(self, text: str) -> str:
        who = f"[Speaker: {self.person or 'unknown'}] " if self.a.speaker_id else ""
        tag = f"{self._tag} " if self._tag else ""
        return f"{tag}{who}{text}"

    def _write(self, draft: Draft, turn: _Turn) -> Answer:
        answer = Answer(turn.text, Identified(self.person, None, None) if self.person else NOBODY, 0.0)
        answer.t_asked = time.perf_counter()
        turn.answer = answer
        approvals = self._approvals or NOBODY_TO_ASK
        approve = approvals.check(self.person, turn.text)
        request = Request(self.person, None, by_web=True, heard=turn.text, may_act=draft.may_act, approve=approve)
        try:
            answer.written = Reply(turn.brain, turn.prompt, request, may_skip=False)
        except BaseException as e:
            if turn.cut and turn.went:  # it stays in the conversation that they asked
                turn.brain.remember_exchange(turn.prompt, CUT)
            elif not turn.cut:
                self._events.put(("failed", turn, e))
            raise
        threading.Thread(target=usage.carried(self._voice), args=(turn,), daemon=True, name="call-reply").start()
        return answer

    def _voice(self, turn: _Turn) -> None:
        """Makes a reply's audio as it's written, told to the loop. The voice is kept going from one sentence into the
        next where it can be (Voice.along), as a person's is; a line made ahead is played as it is."""
        try:
            sentences: queue.Queue = queue.Queue()
            threading.Thread(
                target=usage.carried(self._read), args=(turn, sentences), daemon=True, name="call-reading"
            ).start()
            held: list[str] = []
            at = 0.0  # how far in, in seconds, what's been made reaches
            while (sentence := held.pop() if held else self._sentence(sentences)) is not None:
                turn.answer.sentences.append((time.perf_counter(), sentence))
                made = self._made(sentence)
                if made is not None:
                    self._tell(turn, sentence)
                    at += self._sound(turn, sentence, made, at)
                    continue
                along = getattr(self.a.voice, "along", None)
                if along is None:
                    self._tell(turn, sentence)
                    at += self._sound(turn, sentence, self.a.voice.stream(sentence), at)
                else:
                    at += self._along(turn, along, sentence, sentences, held, at)
                if turn.stop.is_set():
                    return
            self._events.put(("made", turn))
        except Exception as e:  # noqa: BLE001 - said on the call; the next request may work
            if not turn.stop.is_set():
                self._events.put(("failed", turn, e))

    def _read(self, turn: _Turn, sentences: queue.Queue) -> None:
        try:
            for sentence in split_sentences(turn.answer.written):
                if turn.stop.is_set():
                    break
                sentences.put(sentence)
            sentences.put(None)
        except Exception as e:  # noqa: BLE001 - raised where the sentences are taken
            sentences.put(e)

    @staticmethod
    def _sentence(sentences: queue.Queue, timeout: float | None = None) -> str | None:
        item = sentences.get(timeout=timeout)
        if item is None or isinstance(item, Exception):
            sentences.put(item)  # the end, for whoever asks next
            if item is not None:
                raise item
        return item

    def _tell(self, turn: _Turn, sentence: str) -> None:
        self._events.put(("sentence", turn, sentence))

    def _sound(self, turn: _Turn, sentence: str, audio, at: float) -> float:
        """One sentence's audio, its words spread over it. How long it lasts."""
        pending, seconds = b"", 0.0
        try:
            for chunk in audio:
                if turn.stop.is_set():
                    return seconds
                pending += chunk
                usable = len(pending) // 2 * 2
                if usable:
                    self._events.put(("sound", turn, pending[:usable]))
                    seconds += usable / 2 / self._rate
                pending = pending[usable:]
        except Exception as e:
            mark_failed_at(e, TTS)
            raise
        finally:
            if close := getattr(audio, "close", None):
                close()
        spread = sentence.split()
        self._events.put(("words", turn, [(w, at + seconds * (i + 1) / len(spread)) for i, w in enumerate(spread)]))
        return seconds

    def _along(self, turn: _Turn, along, first: str, sentences: queue.Queue, held: list[str], at: float) -> float:
        """`first` and the sentences that follow it soon enough, in one breath."""
        told: list[str] = []

        def more() -> str | None:
            """The next sentence for the same breath; None: none yet, or one made ahead, or no more."""
            if turn.stop.is_set():
                return None
            try:
                sentence = self._sentence(sentences, NEXT_SENTENCE_S)
            except queue.Empty:
                return None
            except Exception:  # noqa: BLE001 - the next to ask raises it
                return None
            if sentence is None:
                return None
            if self._made(sentence) is not None:
                held.append(sentence)
                return None
            turn.answer.sentences.append((time.perf_counter(), sentence))
            told.append(sentence)
            self._tell(turn, sentence)
            return sentence

        self._tell(turn, first)
        seconds, pending = 0.0, b""
        pieces = along(first, more)
        try:
            for piece in pieces:
                if turn.stop.is_set():
                    break
                if isinstance(piece, bytes):
                    pending += piece
                    usable = len(pending) // 2 * 2
                    if usable:
                        self._events.put(("sound", turn, pending[:usable]))
                        seconds += usable / 2 / self._rate
                    pending = pending[usable:]
                else:
                    self._events.put(("words", turn, [(w, at + end) for w, end in piece]))
        except ServiceError as e:
            if seconds:
                mark_failed_at(e, TTS)
                raise
            print(f"(call: the voice couldn't go on in one breath ({e}); a sentence at a time)", flush=True)
            for sentence in (first, *told):
                seconds += self._sound(turn, sentence, self.a.voice.stream(sentence), at + seconds)
        finally:
            pieces.close()
        return seconds

    def _throw_away(self, turn: _Turn, answer: Answer) -> None:
        """A cancelled draft's, before any later one starts. One the caller heard some of, or at least asked for,
        stays in the conversation as far as it was heard, with what it sent and set."""
        brain = turn.brain
        brain.interrupt()
        sent, changes = list(brain.sent), list(brain.changes)
        by, service = brain.answered_by, brain.quick_service
        answer.written.drop()
        if not turn.went:
            return
        brain.remember_exchange(turn.prompt, f"{turn.heard} {CUT}".strip())
        if turn.heard:
            self._keep(turn, f"{turn.heard} {CUT}", sent, changes, by, service)

    def _go(self, text: str) -> None:
        """They're done: the answer is played as soon as there is one."""
        self._interrupt()
        if not words(text):
            self._drop_draft()
            return
        self.a.journal.heard(None, text, self.person, None, None, first=False)
        print(f"You (call):  {text!r}", flush=True)
        if usage.capped():
            self._drop_draft()
            self._say(usage.CAPPED_LINE, only_made=True)
            return
        if says_bye(text):
            self._drop_draft()
            self.a.brain.remember_exchange(self._prompt(text), GOODBYE)
            self._goodbye()
            return
        if self._draft and self._draft.text == text:
            self.counts["early"] += 1
        self._prepare(text)
        turn, self._draft, self._tag = self._draft, None, ""
        turn.waited = getattr(self._flux, "waited", None)
        turn.went, turn.ended_at, turn.ended_pc = True, self._clock(), time.perf_counter()
        self._now = turn
        turn.draft.go()

    def _drop_draft(self) -> None:
        turn, self._draft = self._draft, None
        if turn:
            turn.cut = True
            turn.stop.set()
            turn.draft.cancel()
            turn.brain.interrupt()

    # Being heard

    def _send_ready(self) -> None:
        """What's made of the turn and not sent yet, on its way, with a mark every MARK_S to tell how far it played."""
        turn = self._now
        if turn is None or not turn.went or turn.paused_at is not None:
            return
        if len(turn.audio) > turn.sent:
            now = self._clock()
            if turn.first_sound is None:
                turn.first_sound = time.perf_counter()
            if not turn.playing:
                turn.playing = True
                turn.last_mark = turn.sent
                self.link.send({"type": "play", "id": next(self._ids), "listening": True})
                turn.anchor = (turn.sent, now + SENT_AHEAD_S)
            elif self._heard_to(turn) >= turn.sent:  # it ran dry: what comes now is heard from now
                turn.anchor = (turn.sent, now + SENT_AHEAD_S)
            step = int(MARK_S * self._rate) * 2
            while turn.sent < len(turn.audio):
                upto = min(len(turn.audio), turn.last_mark + step)
                self.link.send(bytes(turn.audio[turn.sent : upto]))
                turn.sent = upto
                if upto - turn.last_mark >= step:
                    self._mark(turn)
        if turn.made and turn.playing and turn.last_mark < turn.sent:
            self._mark(turn)

    def _mark(self, turn: _Turn) -> None:
        mark = next(self._ids)
        turn.marks[mark], turn.last_mark = turn.sent, turn.sent
        self.link.send({"type": "mark", "id": mark})

    def _heard_to(self, turn: _Turn) -> int:
        """How far into the turn's audio the caller has heard, in bytes."""
        if not turn.playing:
            return turn.anchor[0] if turn.paused_at is not None else 0
        at, when = turn.anchor
        return min(turn.sent, at + int(max(0.0, self._clock() - when) * self._rate) * 2)

    def _note_played(self) -> None:
        turn = self._now
        while not self.link.played.empty():
            mark, cut = self.link.played.get_nowait()
            if turn and turn.playing and not cut and (upto := turn.marks.pop(mark, None)) is not None:
                turn.anchor = (upto, self._clock())
        if turn is None or not turn.made or turn.paused_at is not None or turn.sent < len(turn.audio):
            return
        if turn.marks or turn.last_mark < turn.sent:
            over_for = self._clock() - turn.anchor[1] - (turn.sent - turn.anchor[0]) / 2 / self._rate
            if over_for < LOST_MARK_S:
                return
        self._said(turn)

    def _said(self, turn: _Turn) -> None:
        """All of it was heard."""
        self._now, self._last, self._last_spoken = None, self._clock(), turn.spoken
        if turn.draft:
            turn.heard = turn.spoken
            self._settle(turn)
        if turn.playing and self._flux and (terms := offered(turn.spoken)):
            self._flux.expect(terms)
        if turn.ends_call:
            self._over.set()
            try:
                self.link.send({"type": "hangup"})
            except BodyGone:
                pass

    def _settle(self, turn: _Turn) -> None:
        """A reply kept whole."""
        brain = turn.brain
        sent, changes = list(brain.sent), list(brain.changes)
        by, service = brain.answered_by, brain.quick_service
        turn.draft.keep()
        if turn.later:
            self.a.brain.remember_said(turn.spoken)
        self._keep(turn, turn.spoken, sent, changes, by, service)

    def _pause(self, turn: _Turn) -> None:
        if turn.paused_at is not None or not turn.playing:
            return
        self._rewind(turn, self._heard_to(turn))
        turn.paused_at = self._clock()

    def _rewind(self, turn: _Turn, upto: int) -> None:
        """Stops the sound; sending goes on from the start of the word they were hearing."""
        self.link.send({"type": "stop"})
        ends = [end for _, end in turn.words if end * 2 * self._rate <= upto]
        turn.sent = min(upto, int(ends[-1] * self._rate) * 2) if ends else 0
        turn.playing, turn.last_mark, turn.anchor = False, turn.sent, (turn.sent, self._clock())
        turn.marks.clear()

    def _resume(self, turn: _Turn) -> None:
        if turn.paused_at is not None:
            turn.paused_at = None
            self.counts["resumed"] += 1
            print("(call: nothing came of it; going on)", flush=True)

    def _resume_if_nothing_came(self) -> None:
        turn = self._now
        if turn and turn.paused_at is not None:
            since = self._clock() - max(turn.paused_at, self._last)
            if since > RESUME_S:
                self._ignoring = True
                self._resume(turn)

    def _interrupt(self) -> None:
        """Stops what TARS is saying, or about to."""
        turn, self._now = self._now, None
        if turn is None:
            return
        upto = self._heard_to(turn)
        if turn.audio and upto >= len(turn.audio) and turn.made:
            turn.heard = turn.spoken
        else:
            turn.heard = " ".join(w for w, end in turn.words if end * 2 * self._rate <= upto)
        turn.cut = True
        turn.stop.set()
        if turn.playing and not self.link.gone.is_set():
            try:
                self.link.send({"type": "stop"})
            except BodyGone:
                pass
        self._last_spoken = turn.heard
        if turn.draft:
            self.counts["interrupted"] += 1
            self._tag = INTERRUPTED if turn.heard else UNHEARD
            turn.draft.cancel()
            turn.brain.interrupt()
            if turn.later and turn.heard:  # its own brain isn't the call's any more
                self.a.brain.remember_said(f"{turn.heard} {CUT}")

    def _keep(self, turn: _Turn, text: str, sent, changes, by, service) -> None:
        """What was said, in the journal, and what it sent and set, done."""
        answer = turn.answer
        timings = {"head_start": max(0.0, turn.ended_pc - answer.t_asked)}
        if answer.sentences and turn.first_sound:
            written = answer.sentences[0][0]
            timings["llm"] = written - answer.t_asked
            timings["tts"] = max(0.0, turn.first_sound - written)
            if not turn.later:
                timings["end_of_speech"] = turn.waited or 0.0
                timings["total"] = turn.first_sound - turn.ended_pc + timings["end_of_speech"]
                self.totals.append(timings["total"])
                waited = "unknown" if turn.waited is None else f"{turn.waited:.2f}s"
                print(
                    f"(call: first sound {timings['total']:.2f}s after their last word; {waited} of it to be sure they "
                    f"were done)",
                    flush=True,
                )
        self.a.journal.answered(text, sent, asker=self.person, timings=timings, answered_by=by, quick_service=service)
        self._change(turn, changes)

    def _change(self, turn: _Turn, changes: list) -> None:
        """What a reply asked of the reminders: their own are set now, and one for someone else once they approve."""
        others = [c for c in changes if getattr(c, "reminder", None) and for_other(c.reminder.for_name, self.person)]
        self.a._change_reminders([c for c in changes if c not in others], self.person)
        for c in others:
            new = c.reminder
            at = f"\nat: {time.strftime('%a %H:%M', time.localtime(new.due))}" if new.due else "\nwhen they're back"
            what = f"{new.kind} for {new.for_name}{at}\n{new.text or ''}"

            def do(change=c) -> str:
                self.a.reminder_tools.apply(change, from_name=self.person)
                self.a._reread_reminders()
                return "Done."

            ask = (self._approvals or NOBODY_TO_ASK).ask
            # On a thread of its own: Telegram never holds the call up.
            threading.Thread(target=ask, args=(self.person, turn.text, what, do), daemon=True, name="call-ask").start()

    def _failed(self, turn: _Turn, e: Exception) -> None:
        if turn in self._later:
            self._later.remove(turn)
            turn.draft.cancel()
            return
        if turn.cut or turn is not self._now:
            return
        print(f"(call: couldn't answer: {e!r})", flush=True)
        self.a.journal.failed(turn.spoken, failed_at(e), f"{type(e).__name__}: {e}")
        self._interrupt()
        self._tag = ""
        if isinstance(e, usage.Capped):
            self._say(usage.CAPPED_LINE, only_made=True)
            return
        made = [line for line in self.a.error_lines if self._made(line)]
        if made:
            self._say(random.choice(made))

    # While something's slow

    def _fill(self) -> None:
        """A word when a reply is slow, so the line isn't dead; never over anyone."""
        turn = self._now
        if turn is None or turn.draft is None or turn.made or self._talking or turn.paused_at is not None:
            return
        if turn.fills >= MAX_FILLS or turn.sent < len(turn.audio) or self._heard_to(turn) < turn.sent:
            return
        since = turn.anchor[1] + (turn.sent - turn.anchor[0]) / 2 / self._rate if turn.playing else turn.ended_at
        if self._clock() - since < (STILL_S if turn.audio else SLOW_S) or self._clock() - since < 0:
            return
        line = STILL_ON_IT if turn.audio else ONE_MOMENT
        turn.fills += 1
        if (audio := self._made(line)) is None:
            return
        sound = b"".join(audio)
        shift = len(sound) / 2 / self._rate
        start = len(turn.audio) / 2 / self._rate
        turn.audio += sound
        # What's made after it lands that much later: its words are told by where the reply's own audio would be.
        turn.words += [(w, start + shift) for w in line.split()]
        turn.said.append(line)
        turn.shifted += shift

    def _later_if_slow(self, turn: _Turn, text: str) -> None:
        """Something slow they've talked over goes on by itself, unless they called it off."""
        said = words(text)
        slow = turn.draft and turn.went and not turn.real and self._clock() - turn.ended_at > DETACH_S
        if not slow or turn.later or self._new_brain is None or len(self._later) >= MAX_LATER:
            return
        if turn.answer is None or (said and said[0] in CANCELS):
            return
        busy, turn.later = turn.brain, True
        fresh = self._new_brain()
        fresh.carry_over(busy)
        fresh.remember_exchange(turn.prompt, STILL_ON_IT)
        self.a.brain, self._thinking = fresh, threading.Lock()
        if turn.playing and not self.link.gone.is_set():
            self.link.send({"type": "stop"})
        # What was said of it so far was only "One moment": the rest is said on its own.
        dropped = len(turn.audio) / 2 / self._rate
        turn.audio.clear()
        turn.said.clear()
        turn.words.clear()
        turn.marks.clear()
        turn.sent, turn.playing, turn.paused_at, turn.first_sound = 0, False, None, None
        turn.shifted -= dropped
        turn.last_mark, turn.anchor = 0, (0, 0.0)
        self._now = None
        self._later.append(turn)
        self.counts["later"] += 1
        print(f"(call: finishing {turn.text!r} meanwhile)", flush=True)

    def _say_what_was_found(self) -> None:
        if self._now or self._talking or self._draft or self._clock() - self._last < QUIET_GAP_S:
            return
        for turn in self._later:
            if turn.made and turn.said:
                self._later.remove(turn)
                if (audio := self._made(FOUND)) is not None:
                    sound = b"".join(audio)
                    shift = len(sound) / 2 / self._rate
                    turn.audio[:0] = sound
                    turn.words = [(FOUND, shift), *((w, end + shift) for w, end in turn.words)]
                self._now = turn
                return

    def _send_what_was_found(self) -> None:
        """The call is over: what's still being found goes to their chat and the TARS page when it's done."""
        for turn in self._later:
            threading.Thread(target=self._send_later, args=(turn,), daemon=True, name="call-later").start()

    def _send_later(self, turn: _Turn) -> None:
        deadline = time.monotonic() + LATER_WAIT_S
        while not turn.made and time.monotonic() < deadline:
            try:
                self._take(*self._events.get(timeout=0.2))
            except queue.Empty:
                continue
            except Exception as e:  # noqa: BLE001 - the call's own last events
                print(f"(call: {e!r})", flush=True)
        if not turn.made:
            turn.stop.set()
            turn.draft.cancel()
            turn.brain.interrupt()
            return
        brain = turn.brain
        sent, changes = list(brain.sent), list(brain.changes)
        turn.draft.keep()
        note = SentItem(NOTE, turn.text[:80], body=turn.spoken)
        self.a.journal.sent([*sent, note], self.person)
        self._change(turn, changes)

    # Lines of its own

    def _made(self, line: str) -> list[bytes] | None:
        made = self.a.phrases.made_ahead(line)
        return made.result() if made and made.done() and not made.exception() else None

    def _say(self, line: str, ends_call: bool = False, only_made: bool = False) -> None:
        turn = _Turn(ends_call=ends_call, went=True, ended_at=self._clock())
        self._now = turn

        def say() -> None:
            try:
                audio = self._made(line)
                if audio is None and not only_made:
                    audio = self.a.phrases.get(line).result(timeout=PHRASE_WAIT_S)
                if audio:
                    self._tell(turn, line)
                    self._sound(turn, line, audio, 0.0)
            except Exception as e:  # noqa: BLE001 - a line it can't say is skipped
                print(f"(call: couldn't say {line!r}: {e!r})", flush=True)
            self._events.put(("made", turn))

        threading.Thread(target=usage.carried(say), daemon=True, name="call-line").start()

    def _goodbye(self) -> None:
        if self._now and self._now.ends_call:
            return
        self._interrupt()
        self._say(GOODBYE, ends_call=True)
