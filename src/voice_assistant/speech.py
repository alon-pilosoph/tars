"""Turn a streamed LLM reply into continuous audio, one sentence at a time."""

import queue
import re
import threading
from collections.abc import Callable, Iterable, Iterator

from .tts import Voice

# A sentence ends at . ! or ? followed by whitespace (so "3.5" doesn't split), or at a newline.
SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")
# Markdown the model sometimes emits despite being told not to; it sounds wrong read aloud.
MARKDOWN = re.compile(r"[*_#`]+")

_DONE = object()


# Web search answers carry their sources inline, as "([site](url))": for the screen, never for reading out.
_URL_IN_LINK = r"\((?:[^()\s]|\([^()\s]*\))*\)"  # the (url) part, which may hold one level of (brackets)
CITATION = re.compile(r"\s*\(\s*\[[^\]]*\]" + _URL_IN_LINK + r"\s*\)")
LINK = re.compile(r"\[([^\]]*)\]" + _URL_IN_LINK)  # [words](url): say the words
URL = re.compile(r"\s*\bhttps?://\S+")


def clean_for_speech(text: str) -> str:
    text = URL.sub("", LINK.sub(r"\1", CITATION.sub("", text)))
    return MARKDOWN.sub("", text).strip()


def split_sentences(pieces: Iterable[str]) -> Iterator[str]:
    buffer = ""
    for piece in pieces:
        buffer += piece
        while match := _sentence_end(buffer):
            sentence, buffer = clean_for_speech(buffer[: match.end()]), buffer[match.end() :]
            if sentence:
                yield sentence
    if sentence := clean_for_speech(buffer):
        yield sentence


def _sentence_end(text: str) -> re.Match | None:
    """The first sentence end that isn't inside a [link](...), which only makes sense cleaned whole."""
    for match in SENTENCE_END.finditer(text):
        before = text[: match.end()]
        if before.rfind("[") <= before.rfind(")"):
            return match
    return None


class _Prefetch:
    """Starts synthesizing one sentence immediately in the background; iterate to get its audio in order."""

    def __init__(self, voice: Voice, text: str):
        self._chunks: queue.Queue = queue.Queue()
        threading.Thread(target=self._fetch, args=(voice, text), daemon=True).start()

    def _fetch(self, voice: Voice, text: str) -> None:
        try:
            for chunk in voice.stream(text):
                self._chunks.put(chunk)
        except Exception as e:  # noqa: BLE001 - raised again where the audio is played
            self._chunks.put(e)
        self._chunks.put(_DONE)

    def __iter__(self) -> Iterator[bytes]:
        while (item := self._chunks.get()) is not _DONE:
            if isinstance(item, Exception):
                raise item
            yield item


class StreamedReply:
    """A reply's audio as one continuous PCM stream, made from the moment this is created.

    The LLM stream is read on a background thread; each sentence is sent to TTS the moment it's complete, so later
    sentences synthesize while earlier ones are still playing, and a reply prepared ahead of time is ready to play.
    """

    def __init__(self, pieces: Iterable[str], voice: Voice, on_sentence: Callable[[str], None]):
        self._playlist: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._producer = threading.Thread(target=self._produce, args=(pieces, voice, on_sentence), daemon=True)
        self._producer.start()

    def _produce(self, pieces: Iterable[str], voice: Voice, on_sentence: Callable[[str], None]) -> None:
        try:
            for sentence in split_sentences(self._until_stopped(pieces)):
                on_sentence(sentence)
                self._playlist.put(_Prefetch(voice, sentence))
        except Exception as e:  # noqa: BLE001 - raised again where the audio is played
            self._playlist.put(e)
        self._playlist.put(_DONE)

    def _until_stopped(self, pieces: Iterable[str]) -> Iterator[str]:
        for piece in pieces:
            if self._stop.is_set():
                return
            yield piece

    def stop(self) -> None:
        """Stop at the next piece of the reply; interrupt the brain to get there sooner. Doesn't wait: a reply
        stuck on the network (a web search) finishes on its own, and nothing it makes is played."""
        self._stop.set()

    def __iter__(self) -> Iterator[bytes]:
        while (item := self._playlist.get()) is not _DONE:
            if isinstance(item, Exception):
                raise item
            yield from item
