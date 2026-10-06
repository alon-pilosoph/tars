"""What the assistant records as it works: every wake and near-miss, what followed, and every conversation.

Writing must never cost a reply. Every write catches its own failure (disk full, database locked by the web UI),
prints it and carries on, and what someone said is written while TARS answers. Writes run in order on one worker
thread; any call that reads their results waits for them first. Without an event log nothing is kept.
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
        self._wake: int | None = None  # id of the logged wake being answered
        self._said: list[tuple[float, str]] = []  # "Yes, Alon?" / "Did you call me?": kept only if someone answers
        self._conversation: int | None = None
        self._gone = False  # the conversation was deleted in the web UI while it went on: keep no more of it
        self._first_pending = False  # the wake's request was heard; its answer says whether the wake was real
        self._turn: int | None = None  # id of the last person turn kept in the conversation
        # Near-misses are written off the mic loop and requests off the reply's path: a locked database can stall a
        # write for seconds.
        self._background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal")

    @property
    def conversation_id(self) -> int | None:
        """The conversation being kept, once something in it was written (call flush() first)."""
        return None if self._gone else self._conversation

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
        if self.events:
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
        self.flush()
        self._wake = None
        if self.events:
            self._wake = self._safe(
                self.events.add_wake, pcm, score, outcome, heard, confidence, wake_model, check_model
            )

    def said(self, text: str) -> None:
        """TARS spoke before anyone asked anything."""
        self._said.append((time.time(), text))

    def nobody_spoke(self) -> None:
        self.flush()
        self._nobody_spoke()

    def _nobody_spoke(self) -> None:
        if self._wake is not None:
            self._safe(self.events.set_follow, self._wake, SAID_NOTHING)
        self._said = []

    def heard(
        self, pcm: bytes, text: str, name: str | None, score: float | None, embedding: np.ndarray | None, first: bool
    ) -> None:
        """Someone spoke (`first`: right after the wake). Written in the background, so TARS can answer meanwhile."""
        if self.events:
            said, self._said = self._said, []
            self._background.submit(self._heard, time.time(), said, pcm, text, name, score, embedding, first)

    def _heard(
        self,
        ts: float,
        said: list[tuple[float, str]],
        pcm: bytes,
        text: str,
        name: str | None,
        score: float | None,
        embedding: np.ndarray | None,
        first: bool,
    ) -> None:
        self._turn = None
        audio = None
        if first and self._wake is not None:
            audio = self._safe(self.events.add_request, self._wake, pcm, text, name, score, embedding)
        if not text:
            if first:
                self._nobody_spoke()
            return
        self._first_pending = first
        if self.conversations is None or self._gone:
            return
        if self._conversation is None:
            self._conversation = self._safe(self._start, said, ts)
        if (conversation := self._conversation) is None:
            return
        self._turn = self._safe(self.conversations.add_person_turn, conversation, text, pcm, name, audio=audio, ts=ts)
        if self._turn is None and self._safe(self.conversations.exists, conversation) is False:
            self._gone = True

    def _start(self, said: list[tuple[float, str]], ts: float) -> int:
        conversation = self.conversations.start(self._wake, ts=said[0][0] if said else ts)
        for said_at, text in said:
            self.conversations.add_tars_turn(conversation, text, ts=said_at)
        return conversation

    def answered(
        self,
        text: str,
        sent: list[SentItem],
        asker: str | None,
        timings: dict[str, float] | None = None,
        answered_by: str | None = None,
    ) -> None:
        """Sent items are kept even if the conversation couldn't be. `timings`: seconds by stage
        (conversations.TIMINGS); `answered_by`: which model wrote it (Brain.answered_by)."""
        self.flush()
        self._settle_wake(ASKED)
        if self.conversations is None:
            return
        conversation = None if self._gone else self._conversation
        tars_turn = (
            self._safe(self.conversations.add_tars_turn, conversation, text, timings=timings, answered_by=answered_by)
            if conversation
            else None
        )
        for item in sent:
            # "For whoever asked" needs a known asker; otherwise it's the household's.
            item = item if asker else dataclasses.replace(item, scope=HOUSEHOLD)
            self._safe(self.conversations.add_item, conversation, tars_turn, item, for_name=asker)

    def failed(
        self,
        text: str,
        failed_at: str,
        error: str,
        timings: dict[str, float] | None = None,
        answered_by: str | None = None,
    ) -> None:
        """Answering went wrong (`failed_at`: a conversations.STAGES), after saying `text` of the answer, maybe
        nothing. Kept as a TARS turn, so the web UI shows where it failed; when nothing was heard (transcribing
        failed), the conversation starts here."""
        self.flush()
        self._settle_wake(ASKED)
        if self.conversations is None or self._gone:
            return
        if self._conversation is None:
            said, self._said = self._said, []
            self._conversation = self._safe(self._start, said, time.time())
        if self._conversation is None:
            return
        self._safe(
            self.conversations.add_tars_turn,
            self._conversation,
            text,
            timings=timings,
            answered_by=answered_by,
            failed_at=failed_at,
            error=error,
        )

    def not_for_tars(self) -> None:
        """TARS stayed quiet about what was just heard: it was overheard, or a "no" to "Did you call me?"."""
        self.flush()
        self._settle_wake(NOT_FOR_US)
        if self._turn is not None:
            self._safe(self.conversations.mark_not_for_tars, self._turn)

    def _settle_wake(self, follow: str) -> None:
        if self._first_pending and self._wake is not None:
            self._safe(self.events.set_follow, self._wake, follow)
        self._first_pending = False

    def end_conversation(self) -> None:
        self.flush()
        if self._conversation is not None and not self._gone:
            self._safe(self.conversations.end, self._conversation)
        self._wake, self._said, self._conversation, self._gone, self._first_pending = None, [], None, False, False
        self._turn = None

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
