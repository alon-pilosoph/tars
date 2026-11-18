"""Ending a conversation by saying so: "thanks", "that's all", "never mind".

Said on its own, it's answered with one short line made ahead, and TARS stops listening for a follow-up, instead of
asking the model and keeping the mic open for more. Not "stop" or "cancel" (they're about something: the music, a
timer) and not "good night" (it may start the house's evening).
"""

import re

THANKS, DONE = "thanks", "done"

_FILLER = r"(?:hey\s+)?(?:tars|ok|okay|oh|great|perfect|cool|awesome|nice|good|alright|all right|got it|yes|yeah)"
_PATTERNS = {
    THANKS: r"(?:(?:thanks|thank\s+you|thank\s+you\s+so\s+much|thanks\s+a\s+lot|cheers|much\s+appreciated)"
    r"(?:\s+(?:tars|buddy|man|mate))?)",
    DONE: r"(?:never\s?mind|that'?s\s+(?:all|it|everything)|nothing(?:\s+else)?|no\s+thanks|no\s+thank\s+you|"
    r"i'?m\s+good|i'?m\s+done|we'?re\s+done|bye|goodbye|see\s+you|that\s+will\s+be\s+all|forget\s+it)",
}

LINES = {
    "dry": {THANKS: "Anytime.", DONE: "Okay."},
    "plain": {THANKS: "You're welcome.", DONE: "Okay."},
}


def closing(text: str) -> str | None:
    """THANKS or DONE when `text` is nothing but a way to end the conversation ("thanks", "ok thanks TARS",
    "never mind", "that's all, thank you"), else None. Anything more ("thanks, and the lights") is a request."""
    words = re.sub(r"[^\w\s']", " ", text.lower().replace("’", "'"))
    words = " ".join(words.split())
    if not words:
        return None
    found = None
    rest = words
    for kind in (DONE, THANKS):  # "no thanks" is done, not thanks
        rest, n = re.subn(rf"\b{_PATTERNS[kind]}\b", " ", rest)
        if n and found is None:
            found = kind
    rest = re.sub(rf"\b{_FILLER}\b", " ", rest)
    rest = re.sub(r"\b(?:and|so|then|that's|that\s+was|it|very|much|really|again|for\s+that|you)\b", " ", rest)
    return found if found and not rest.split() else None
