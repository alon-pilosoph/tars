"""Whether a chat message that comes after a quiet spell carries on the conversation before it, or starts a new one
(docs/telegram.md#one-conversation-or-a-new-one). A fast model reads the last few turns and the new message, and says
one word. Asked only once the chat has been quiet for [telegram] memory_minutes: before that it's the same
conversation, as ever, and after [telegram] keep_days it's always a new one."""

import re
from collections.abc import Callable

# What the model is told. Short, and with its answer fixed to one of two words, so it's quick and cheap.
PROMPT = """You sort chat messages. Below is the end of a conversation between a person and TARS, their home \
assistant, then a new message from the person after a quiet spell.

Say "continued" if the new message carries on that conversation: an answer to what TARS asked, a follow-up, a \
correction, or a short message that only makes sense with it ("try again", "and tomorrow?", "thanks", "the second \
one"). Say "new" if it's about something else.

Answer with one word: continued or new."""
RECENT_TURNS = 6  # how many of the last turns the model reads
TURN_CHARS = 400  # each one cut to this


class Continuity:
    """`ask(messages)`: a model's answer to chat `messages`, as text; it may raise, and then the conversation carries
    on (losing the thread is worse than an old one kept)."""

    def __init__(self, ask: Callable[[list[dict]], str]):
        self._ask = ask

    def continues(self, turns: list[tuple[str, str]], message: str, quiet_s: float, person: str) -> bool:
        """Whether `message`, from `person` after `quiet_s` of quiet, carries on the conversation whose turns are
        `turns` ((who, text), oldest first: who is the person's name or "TARS")."""
        if not turns:
            return False
        try:
            said = self._ask(messages(turns, message, quiet_s, person))
        except Exception as e:  # noqa: BLE001 - carried on, and said
            print(f"(Telegram: couldn't tell if {person}'s message carries on the conversation ({e!r}); it does)")
            return True
        verdict = verdict_of(said)
        print(f"(Telegram: {person}'s message after {_quiet(quiet_s)} of quiet: {verdict or said!r})", flush=True)
        return verdict != "new"


def messages(turns: list[tuple[str, str]], message: str, quiet_s: float, person: str) -> list[dict]:
    """The model's request: the instructions, then the last turns and the new message."""
    lines = [f"{who}: {_cut(text)}" for who, text in turns[-RECENT_TURNS:]]
    said = "\n".join(lines)
    later = _quiet(quiet_s).capitalize()
    asked = f"The conversation:\n{said}\n\n{later} later, the new message:\n{person}: {_cut(message)}"
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": asked}]


def verdict_of(said: str) -> str:
    """ "continued" or "new", from the model's answer (its last word, whatever came before it); "" for neither."""
    words = re.findall(r"[a-z]+", said.lower())
    return words[-1] if words and words[-1] in ("continued", "new") else ""


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TURN_CHARS else text[: TURN_CHARS - 1] + "…"


def _quiet(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1.5:
        return "an hour"
    if hours < 36:
        return f"{round(hours)} hours"
    return f"{round(hours / 24)} days"
