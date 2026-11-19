"""What comes with a message for the brain to see or read: a photo, a PDF, a text file, sent on Telegram (and, later,
forwarded by mail). The brain's model (OpenAI's, which sees images and reads PDFs) gets each as part of the message;
a quick model, which can't, never answers a conversation that has one (llm.QuickChat).

What's in one is words from anywhere, like a web page's: a reply that has one in front of it reads the house but
changes nothing in it (home.Request.read_the_web), and the brain is told its words are data, never instructions.
"""

import base64
from dataclasses import dataclass

IMAGE, PDF, TEXT = "image", "pdf", "text"
# What the model takes as an image. HEIC (an iPhone's camera, sent as a file) isn't one: sent as a photo, Telegram
# turns it into a JPEG.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
TEXT_ENDINGS = (".txt", ".md", ".csv", ".json", ".ics", ".vcf", ".log", ".xml", ".html", ".htm", ".yaml", ".yml")
MAX_BYTES = 20_000_000  # what Telegram lets a bot fetch, and well inside what the model takes of a file
MAX_TEXT_CHARS = 60_000  # about 15k tokens: a long letter or a big list, not a book
PHOTO_NAME = "photo.jpg"  # a photo sent as one (not as a file), which Telegram doesn't name
KEEP_TURNS = 3  # a photo stays in front of the model for its own turn and this many more of theirs, then only its name
# For a conversation aloud, with people who have a chat: what TARS can't see from where it is, it asks to be sent.
ASK_FOR_A_PHOTO = (
    "On Telegram, {people} can send you photos and files (PDFs, text files), and you see and read them there. When "
    "answering someone with a chat properly needs you to see or read something you can't from here (a rash, a "
    "label, a letter or a bill, an error on a screen, something broken), don't guess: say so in a short line, and "
    "ask them to send you a photo of it on Telegram. Anyone else, ask to describe it."
)


def ask_for_a_photo(people) -> str:
    """ASK_FOR_A_PHOTO for `people`, those with a chat whose model sees; "" with none."""
    return ASK_FOR_A_PHOTO.format(people=", ".join(people)) if people else ""


@dataclass(frozen=True)
class Attached:
    """One thing sent with a message. `kind`: IMAGE, PDF or TEXT. `name`: its file name, or "photo.jpg"."""

    kind: str
    name: str
    mime: str
    data: bytes

    def part(self) -> dict:
        """As OpenAI's Responses API takes it, in a message's content."""
        if self.kind == IMAGE:
            url = f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"
            return {"type": "input_image", "image_url": url, "detail": "high"}  # high: a label's small print
        if self.kind == PDF:
            data = f"data:application/pdf;base64,{base64.b64encode(self.data).decode()}"
            return {"type": "input_file", "filename": self.name, "file_data": data}
        words = self.data.decode("utf-8", errors="replace")
        cut = len(words) > MAX_TEXT_CHARS
        words = words[:MAX_TEXT_CHARS] + "\n[... the rest isn't shown]" * cut
        return {"type": "input_text", "text": f"The file {self.name}, as it was sent:\n<<<\n{words}\n>>>"}

    @property
    def tag(self) -> str:
        """How the message says it's there: [Photo], or [File: name]."""
        return "[Photo]" if self.kind == IMAGE and self.name == PHOTO_NAME else f"[File: {self.name}]"


def kind_of(mime: str, name: str) -> str | None:
    """IMAGE, PDF or TEXT for a file sent as `mime` named `name`; None for one the model can't take."""
    mime, name = (mime or "").lower(), (name or "").lower()
    if mime in IMAGE_TYPES:
        return IMAGE
    if mime == "application/pdf" or name.endswith(".pdf"):
        return PDF
    if mime.startswith("text/") or name.endswith(TEXT_ENDINGS):
        return TEXT
    return None


def with_parts(text: str, attached: tuple[Attached, ...]) -> str | list[dict]:
    """A user message's content: `text` alone, or with what came with it."""
    if not attached:
        return text
    return [{"type": "input_text", "text": text}, *(a.part() for a in attached)]


def has_parts(item: dict) -> bool:
    """A message in the history that still carries something to see or read."""
    return isinstance(item.get("content"), list)


def put_away(history: list[dict], keep: int = KEEP_TURNS) -> None:
    """Each message in `history` (in place) that came with something more than `keep` of theirs ago keeps only its
    words, and that it came with something: sent again on every turn, a photo would cost its tokens each time, and
    by then the replies have said what it was."""
    theirs = [item for item in history if item.get("role") == "user"]
    for item in theirs[: max(len(theirs) - keep - 1, 0)]:
        if has_parts(item):
            words = item["content"][0]["text"]  # with_parts puts them first
            item["content"] = f"{words} (what came with it is no longer at hand)"
