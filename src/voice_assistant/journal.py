"""What the assistant writes down as it works: every wake and near-miss, what followed, and every conversation.

Writing must never cost a reply: every write here catches its own failure (disk full, the database locked by the
web UI), prints it, and carries on. With logging off (no event log) it keeps nothing.
"""

import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .conversations import HOUSEHOLD, ConversationLog, SentItem
from .events import ASKED, DAY_S, NOT_FOR_US, SAID_NOTHING, EventLog


class Journal:
    def __init__(self, events: EventLog | None = None):
        self.events = events
        self.conversations = ConversationLog(events) if events else None
        self._wake: int | None = None  # the logged wake being answered
        self._said: list[tuple[float, str]] = []  # "Yes, Alon?" / "Did you call me?": kept only if someone answers
        self._conversation: int | None = None
        self._gone = False  # the conversation was deleted in the web UI while it went on: keep no more of it
        self._first_pending = False  # the wake's request was heard; its answer says whether the wake was real
        # Near-misses are written off the mic loop: a locked database can stall a write for seconds.
        self._background = ThreadPoolExecutor(max_workers=1)

    def _safe(self, write, *args, **kwargs):
        if self.events is None:
            return None
        try:
            return write(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - any failure to write, by design
            print(f"(couldn't save this to the log: {e!r})")
            return None

    def near_miss(self, pcm: np.ndarray, peak_score: float, wake_model: str) -> None:
        if self.events:
            self._background.submit(self._safe, self.events.add_near_miss, pcm, peak_score, wake_model, time.time())

    def flush(self) -> None:
        """Wait for the near-misses still being written."""
        self._background.submit(lambda: None).result()

    def wake(
        self,
        pcm: np.ndarray,
        score: float,
        outcome: str,
        heard: str,
        confidence: float | None,
        wake_model: str,
        check_model: str,
    ) -> None:
        self._wake = None
        if self.events:
            self._wake = self._safe(
                self.events.add_wake, pcm, score, outcome, heard, confidence, wake_model, check_model
            )

    def said(self, text: str) -> None:
        """TARS spoke before anyone asked anything."""
        self._said.append((time.time(), text))

    def nobody_spoke(self) -> None:
        if self._wake is not None:
            self._safe(self.events.set_follow, self._wake, SAID_NOTHING)
        self._said = []

    def heard(
        self, pcm: bytes, text: str, name: str | None, score: float | None, embedding: np.ndarray | None, first: bool
    ) -> int | None:
        """Someone spoke (`first`: right after the wake). Returns the turn TARS's answer belongs to, if kept."""
        audio = None
        if first and self._wake is not None:
            audio = self._safe(self.events.add_request, self._wake, pcm, text, name, score, embedding)
        if not text:
            if first:
                self.nobody_spoke()
            return None
        self._first_pending = first
        conversation = self._current_conversation()
        if conversation is None:
            return None
        turn = self._safe(
            self.conversations.add_person_turn, conversation, text, pcm, name, score, embedding, audio=audio
        )
        if turn is None and self._safe(self.conversations.exists, conversation) is False:
            self._gone = True
        return turn

    def _current_conversation(self) -> int | None:
        """A conversation starts when someone first says something, with what TARS already said."""
        if self.conversations is None or self._gone:
            return None
        if self._conversation is None:
            said, self._said = self._said, []
            self._conversation = self._safe(self._start, said)
        return self._conversation

    def _start(self, said: list[tuple[float, str]]) -> int:
        conversation = self.conversations.start(self._wake, ts=said[0][0] if said else None)
        for ts, text in said:
            self.conversations.add_tars_turn(conversation, text, ts=ts)
        return conversation

    def answered(self, turn: int | None, text: str, sent: list[SentItem], asker: str | None) -> None:
        """TARS answered (and maybe sent things). Sent items are kept even if the conversation couldn't be."""
        self._settle_wake(ASKED)
        if self.conversations is None:
            return
        conversation = None if self._gone else self._conversation
        tars_turn = self._safe(self.conversations.add_tars_turn, conversation, text) if conversation else None
        for item in sent:
            # "For whoever asked" only works if we know who asked; otherwise it's the household's.
            item = item if asker else dataclasses.replace(item, scope=HOUSEHOLD)
            self._safe(self.conversations.add_item, conversation, tars_turn, item, for_name=asker)

    def not_for_tars(self, turn: int | None) -> None:
        """TARS stayed quiet: it was overheard, or a "no" to "Did you call me?"."""
        self._settle_wake(NOT_FOR_US)
        if turn is not None:
            self._safe(self.conversations.mark_not_for_tars, turn)

    def _settle_wake(self, follow: str) -> None:
        if self._first_pending and self._wake is not None:
            self._safe(self.events.set_follow, self._wake, follow)
        self._first_pending = False

    def close(self) -> None:
        if self._conversation is not None and not self._gone:
            self._safe(self.conversations.end, self._conversation)
        self._wake, self._said, self._conversation, self._gone, self._first_pending = None, [], None, False, False

    def keep_pruning(self, keep_days: float) -> None:
        """Drop unlabeled audio older than `keep_days` now and once a day (see EventLog.prune_audio); 0 = never."""
        if not self.events or keep_days <= 0:
            return

        def loop():
            while True:
                if dropped := self._safe(self.events.prune_audio, keep_days):
                    print(f"(dropped the audio of {dropped} old unlabeled events)")
                time.sleep(DAY_S)

        threading.Thread(target=loop, daemon=True, name="prune-audio").start()
