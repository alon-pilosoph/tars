"""House commands handled on the Pi ([home] commands_on_pi, docs/home.md#house-commands-on-the-pi): "lock the door",
"turn off the boiler", "AC to 23" done at once, with no model, and with the internet down.

The phrases are made from the house's own devices: verbs ("turn on", "switch off", "put on", "lock"), each device by
its name and label, temperatures for a device that takes one, and the polite bits ("please", "can you", "TARS").
A request that's exactly one of them (as speech to text wrote it, or as Vosk heard it from the same list when the
cloud can't be reached) becomes the home tool's own call, with the same checks: done only when the device says so,
and never unlocking, which stays with the model and its challenge. Anything else goes on to the model as ever.

TARS answers in its own voice from a few lines for each outcome, made ahead and kept on disk, never the same one twice
running. The exchange joins the conversation, so "and the boiler" can follow.
"""

import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .home import LOCK, OFF, ON, SET

DRY_FROM_HUMOR = 50  # [llm] humor, percent: the replies with a joke in them, from here on
ON_PI = "pi"  # answered_by for a turn handled here, as the turns table keeps it
TEMPERATURES = range(16, 31)
NUMBER_WORDS = {
    16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty", 21: "twenty one",
    22: "twenty two", 23: "twenty three", 24: "twenty four", 25: "twenty five", 26: "twenty six",
    27: "twenty seven", 28: "twenty eight", 29: "twenty nine", 30: "thirty",
}  # fmt: skip
POLITE_BEFORE = ("", "please ", "can you ", "could you ", "tars ", "hey tars ")
POLITE_AFTER = ("", " please")
VERBS = {
    ON: ("turn on {d}", "turn {d} on", "switch on {d}", "switch {d} on", "put on {d}", "put {d} on"),
    OFF: ("turn off {d}", "turn {d} off", "switch off {d}", "switch {d} off"),
    LOCK: ("lock {d}",),
}
SET_TEMPERATURE = ("set {d} to {t}", "{d} to {t}", "turn {d} to {t}", "put {d} on {t}", "set {d} to {t} degrees",
                   "{d} to {t} degrees")  # fmt: skip
DONE = ("done", "sent", "started")
REPLIES = {
    ON: ["{label}'s on.", "{label} on.", "On it. {label}'s on."],
    OFF: ["{label}'s off.", "{label} off.", "Done. {label}'s off."],
    LOCK: ["{label}'s locked.", "Locked.", "Locked. Fort Knox has nothing on us."],
    SET: ["{label} to {t} degrees.", "{t} degrees it is.", "Set. {label} to {t}."],
}
PLAIN = {ON: ["{label}'s on."], OFF: ["{label}'s off."], LOCK: ["{label}'s locked."], SET: ["{label} to {t} degrees."]}
FAILED = ["The {mid} didn't confirm it. Check it, or ask me again.", "The {mid} didn't answer. Try again."]


@dataclass(frozen=True)
class Command:
    device: str
    action: str
    temperature: int | None = None

    @property
    def args(self) -> dict:
        return {"device": self.device, "action": self.action} | (
            {"temperature": self.temperature} if self.temperature is not None else {}
        )


def normalize(text: str) -> str:
    """Lower case, no punctuation, numbers as digits and spoken numbers too: "Set the AC to twenty-three." is
    "set the ac to 23"."""
    text = re.sub(r"[^\w\s]", " ", text.lower().replace("-", " "))
    # Letters said one by one, as Vosk writes an acronym: "a c" is "ac".
    text = re.sub(r"\b[a-z](?: [a-z]\b)+", lambda m: m[0].replace(" ", ""), " ".join(text.split()))
    for n, words in sorted(NUMBER_WORDS.items(), key=lambda kv: -len(kv[1])):
        text = re.sub(rf"\b{words}\b", str(n), text)
    return re.sub(r"\b(the|my)\b ?", "", text).strip()


@dataclass
class Commands:
    """The phrases for `devices`: (name, label, actions, takes a temperature) each."""

    devices: Iterable[tuple[str, str, tuple[str, ...], bool]]
    phrases: dict[str, Command] = field(default_factory=dict, init=False)

    def __post_init__(self):
        self._acronyms = {label.lower() for _, label, _, _ in self.devices if label.isupper() and label.isalpha()}
        self._names: dict[str, set[str]] = {}  # each device's names, as phrases have them
        for name, label, actions, warm in self.devices:
            for said in {name.lower(), label.lower()}:
                d = normalize(said)
                self._names.setdefault(name, set()).add(d)
                for action, verbs in VERBS.items():
                    if action in actions:
                        for verb in verbs:
                            self._add(verb.format(d=d), Command(name, action))
                if warm and SET in actions:
                    for t in TEMPERATURES:
                        for verb in SET_TEMPERATURE:
                            self._add(verb.format(d=d, t=t), Command(name, SET, t))

    def _add(self, phrase: str, command: Command) -> None:
        phrase = normalize(phrase)
        had = self.phrases.get(phrase)
        if had is not None and had != command:
            self.phrases[phrase] = None  # two devices said the same way: neither, the model asks which
        elif had is None and phrase not in self.phrases:
            self.phrases[phrase] = command

    def match(self, text: str) -> Command | None:
        """The command `text` is, exactly, give or take the polite bits; None for anything else."""
        said = normalize(text)
        for before in POLITE_BEFORE:
            for after in POLITE_AFTER:
                if said.startswith(normalize(before)) and said.endswith(normalize(after)):
                    core = said[len(normalize(before)) : len(said) - len(normalize(after))].strip()
                    if command := self.phrases.get(core):
                        return command
        return None

    def grammar(self) -> list[str]:
        """Every phrase as Vosk is told to listen for it, numbers in words, and "[unk]" for everything else: also
        before and after each phrase, so words around a command ("don't ... yet") are heard as something else rather
        than squeezed into the command. hear() refuses anything with "[unk]" in it. Without the padding, Vosk heard
        "Don't turn on the fire TV yet" as "turn on the fire t v" (on the Pi, from a house recording session)."""
        spoken = set()
        for phrase, command in self.phrases.items():
            if command is None:
                continue
            # With "the" and without: the matcher drops it, but Vosk has to hear it said.
            with_the = {phrase} | {
                re.sub(rf"\b{re.escape(n)}\b", f"the {n}", phrase, count=1) for n in self._names[command.device]
            }
            for said in with_the:
                words = re.sub(r"\d+", lambda m: NUMBER_WORDS.get(int(m[0]), m[0]), said)
                for acronym in self._acronyms:  # Vosk knows words: "AC" is said "a c"
                    words = re.sub(rf"\b{acronym}\b", " ".join(acronym), words)
                spoken |= {f"{b}{words}{a}".strip() for b in POLITE_BEFORE for a in POLITE_AFTER}
        padded = {f"[unk] {s}" for s in spoken} | {f"{s} [unk]" for s in spoken} | {f"[unk] {s} [unk]" for s in spoken}
        return sorted(spoken | padded) + ["[unk]"]


class Replies:
    """What TARS says after a command, in its voice: a few lines an outcome, never the same one twice running."""

    def __init__(self, labels: dict[str, str], dry: bool = True, choose: Callable[[list[str]], str] = random.choice):
        self._labels, self._dry, self._choose = labels, dry, choose
        self._last: str | None = None

    def lines(self, command: Command) -> list[str]:
        """Every line it might say for `command`, to make ahead."""
        return self._options(command, done=True) + self._options(command, done=False)

    def say(self, command: Command, result: str) -> str:
        options = self._options(command, done=result.startswith(DONE))
        fresh = [o for o in options if o != self._last] or options
        self._last = self._choose(fresh)
        return self._last

    def _options(self, command: Command, done: bool) -> list[str]:
        label = self._labels.get(command.device, command.device)
        mid = label if label.isupper() else label.lower()  # "the AC", "the front door"
        table = (REPLIES if self._dry else PLAIN)[command.action] if done else FAILED
        return [line.format(label=label[:1].upper() + label[1:], mid=mid, t=command.temperature) for line in table]


def sentences(line: str) -> list[str]:
    """A reply line as the voice says it, a sentence at a time: each is made ahead on its own."""
    return [s for s in re.split(r"(?<=[.!?])\s+", line.strip()) if s]


@dataclass
class HouseCommands:
    """What the assistant needs: the phrases, the replies, the home tool's call, and `hear(pcm)`, Vosk listening for
    the phrases alone (None without it), for when the cloud's speech to text can't be reached."""

    commands: Commands
    replies: Replies
    call: Callable[[dict, object], str]
    hear: Callable[[bytes], str] | None = None
    local_first: bool = False  # [home] local_first: the turn's speech to text is LocalFirstSession

    def lines(self) -> list[str]:
        """Every sentence a reply may have, to make ahead."""
        commands = {c for c in self.commands.phrases.values() if c is not None}
        return sorted({s for c in commands for line in self.replies.lines(c) for s in sentences(line)})


def vosk_hearing(grammar: list[str], models_dir) -> Callable[[bytes], str]:
    """Vosk, limited to `grammar`: what it heard in 16 kHz audio, "" for none of it ("[unk]")."""
    import json

    from vosk import KaldiRecognizer, Model, SetLogLevel

    from .audio import SAMPLE_RATE
    from .verify import ensure_model

    SetLogLevel(-1)
    model, spec = Model(str(ensure_model(models_dir))), json.dumps(grammar)

    def hear(pcm: bytes) -> str:
        recognizer = KaldiRecognizer(model, SAMPLE_RATE, spec)
        recognizer.AcceptWaveform(pcm)
        text = json.loads(recognizer.FinalResult()).get("text", "")
        return "" if "[unk]" in text else text

    return hear


class LocalFirstSession:
    """[home] local_first: the recording stays on the Pi until it's over. Then Vosk listens for a house command;
    only anything else is sent, whole, to the cloud's speech to text. With no service deciding when the speaker is
    done (turn_state is None), TARS's own rules do (recorder.py)."""

    turn_state = None

    def __init__(self, hear: Callable[[bytes], str], match: Callable[[str], object], whole):
        self._hear, self._match, self._whole = hear, match, whole
        self._audio = bytearray()
        self.local: bool | None = None  # after transcript(): whether it was heard on the Pi alone

    def feed(self, pcm: bytes) -> None:
        self._audio += pcm

    def transcript(self) -> str:
        heard = self._hear(bytes(self._audio))
        if heard and self._match(heard):
            self.local = True
            return heard
        self.local = False
        session = self._whole.session()
        session.feed(bytes(self._audio))
        session.finish()
        try:
            return session.transcript()
        finally:
            session.close()

    def finish(self) -> None:
        pass

    def close(self) -> None:
        self._audio.clear()


class LocalFirstTranscriber:
    """Sessions that hear house commands on the Pi before anything goes to the cloud (LocalFirstSession); `whole`
    transcribes a finished recording (stt.DeepgramFileTranscriber, with its backup)."""

    def __init__(self, hear: Callable[[bytes], str], match: Callable[[str], object], whole):
        self._hear, self._match, self._whole = hear, match, whole

    def session(self) -> LocalFirstSession:
        return LocalFirstSession(self._hear, self._match, self._whole)
