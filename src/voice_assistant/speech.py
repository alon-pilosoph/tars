"""Turn a streamed LLM reply into continuous audio, one sentence at a time."""

import queue
import re
import threading
from collections.abc import Callable, Iterable, Iterator

from .tts import Voice

# A sentence ends at . ! or ? followed by whitespace (so "3.5" doesn't split), or at a newline.
SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")
# The reply's first words go to text to speech at the first clause break, so TARS starts talking sooner; the rest
# goes a sentence at a time, which sounds more natural. A clause needs a few words ("Well," alone sounds clipped).
CLAUSE_END = re.compile(r"[,;:](?=\s)|\s[—–-](?=\s)")
MIN_CLAUSE_WORDS = 3
# Markdown the model sometimes emits despite being told not to; it sounds wrong read aloud.
MARKDOWN = re.compile(r"[*_#`]+")

_DONE = object()


def clean_for_speech(text: str) -> str:
    return MARKDOWN.sub("", text).strip()


def _first_break(buffer: str) -> re.Match | None:
    for match in CLAUSE_END.finditer(buffer):
        if len(buffer[: match.start()].split()) >= MIN_CLAUSE_WORDS:
            return match
    return None


def _next_break(buffer: str, first: bool) -> re.Match | None:
    ends = [m for m in (SENTENCE_END.search(buffer), first and _first_break(buffer)) if m]
    return min(ends, key=lambda m: m.end(), default=None)


def split_sentences(pieces: Iterable[str], first_clause: bool = False) -> Iterator[str]:
    buffer, first = "", first_clause
    for piece in pieces:
        buffer += piece
        while match := _next_break(buffer, first):
            sentence, buffer = clean_for_speech(buffer[: match.end()]), buffer[match.end() :]
            if sentence:
                first = False
                yield sentence
    if sentence := clean_for_speech(buffer):
        yield sentence


class _Prefetch:
    """Starts synthesizing one sentence immediately in the background; iterate to get its audio in order."""

    def __init__(self, voice: Voice, text: str):
        self._chunks: queue.Queue = queue.Queue()
        threading.Thread(target=self._fetch, args=(voice, text), daemon=True).start()

    def _fetch(self, voice: Voice, text: str) -> None:
        try:
            for chunk in voice.stream(text):
                self._chunks.put(chunk)
        except Exception as e:
            self._chunks.put(e)
        self._chunks.put(_DONE)

    def __iter__(self) -> Iterator[bytes]:
        while (item := self._chunks.get()) is not _DONE:
            if isinstance(item, Exception):
                raise item
            yield item


def speak_streamed_reply(
    pieces: Iterable[str],
    voice: Voice,
    on_sentence: Callable[[str], None],
) -> Iterator[bytes]:
    """Yield reply audio as one continuous PCM stream.

    The LLM stream is read on a background thread; each sentence is sent to TTS the moment
    it's complete, so later sentences synthesize while earlier ones are still playing.
    """
    playlist: queue.Queue = queue.Queue()

    def produce() -> None:
        try:
            for sentence in split_sentences(pieces, first_clause=True):
                on_sentence(sentence)
                playlist.put(_Prefetch(voice, sentence))
        except Exception as e:
            playlist.put(e)
        playlist.put(_DONE)

    threading.Thread(target=produce, daemon=True).start()
    while (item := playlist.get()) is not _DONE:
        if isinstance(item, Exception):
            raise item
        yield from item
