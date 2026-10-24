"""What an errand in TARS's browser needs of the person, on Telegram (browser/errands.py's Chats): an order to approve
(the checkout's screenshot, what it commits them to, Order and Cancel buttons), a link to a one-time page for a login
TARS lacks (browser/login_page.py), a sign-in code sent back in the chat, and the errand's answer.

It sees each update before the chats do (TelegramTars.listen_first): a tap on one of its buttons, and a code while a
sign-in waits for one, are its own, and never part of the conversation, the brain or its log."""

import secrets
import threading
from dataclasses import dataclass, field

from .browser.codes import one_time_code
from .browser.login import Answer
from .browser.login_page import LOGIN_WAIT_S, Asking, Field, ask, reachable_address
from .telegram import TelegramError, TelegramTars

APPROVE_WAIT_S = 10 * 60.0  # how long an order waits for a tap on Order or Cancel
CODE_WAIT_S = 5 * 60.0  # how long a sign-in waits for the code a site sent


@dataclass
class _Tap:
    """An Order/Cancel message waiting for its tap: the chat it's in, and what was tapped."""

    chat_id: int
    tapped: threading.Event = field(default_factory=threading.Event)
    answer: str = ""


class TelegramErrands:
    """The errands' Chats, through TARS's Telegram chats."""

    def __init__(self, tars: TelegramTars, login_host: str, login_port: int):
        """`login_host`: where the one-time login page is served ("": this machine's address its phones reach)."""
        self.tars, self._login_at = tars, (login_host, login_port)
        self._codes: dict[int, Answer] = {}  # a chat whose errand waits for a sign-in code, and where it goes
        self._taps: dict[str, _Tap] = {}  # by the nonce on its buttons
        tars.listen_first(self._take)

    def tell(self, person: str, text: str, picture: bytes | None = None) -> None:
        """Posts an errand's answer to `person`'s chat, as part of its conversation."""
        chat = self.tars.chat_for(person)
        if chat is None:
            return
        with chat.busy:
            if picture:
                self.tars.bot.send_photo(chat.chat_id, picture)
            self.tars.post(chat, text)
            print(f"TARS (Telegram, to {chat.person}):  {text}  (errand)", flush=True)
            self.tars.remember(chat, text)

    def approve(self, person: str, draft: str, picture: bytes | None) -> bool:
        """Asks `person` to approve what an errand is about to commit them to: True only for a tap on Order, on
        this very message, from their own chat, within APPROVE_WAIT_S."""
        chat = self.tars.chat_for(person)
        if chat is None:
            return False
        nonce, waiting = secrets.token_hex(8), _Tap(chat.chat_id)
        self._taps[nonce] = waiting
        try:
            with chat.busy:
                if picture:
                    self.tars.bot.send_photo(chat.chat_id, picture)
                buttons = [("Order", f"order:{nonce}"), ("Cancel", f"cancel:{nonce}")]
                message = self.tars.bot.send_buttons(chat.chat_id, draft, buttons)
                self.tars.remember(chat, draft)
            yes = waiting.tapped.wait(APPROVE_WAIT_S) and waiting.answer == "order"
            try:
                self.tars.bot.drop_buttons(chat.chat_id, message)
            except TelegramError:
                pass  # only tidying: a late tap finds no one waiting
            if yes:
                said = "Approved."
            elif waiting.tapped.is_set():
                said = "Cancelled: nothing done."
            else:
                said = "No answer in time, so nothing was done."
            self.tars.post(chat, said, tries=1)
            return yes
        except TelegramError as e:
            print(f"(Telegram: couldn't ask {person} to approve: {e}; nothing done)")
            return False
        finally:
            self._taps.pop(nonce, None)

    def login(self, person: str, site: str, goal: str, fields: list[Field]) -> dict[str, str] | None:
        """`person`'s login for `site`, for each of `fields`, from a one-time page whose link goes to their chat."""
        chat = self.tars.chat_for(person)
        if chat is None:
            return None
        host, port = self._login_at
        host = host or reachable_address()
        why = f"to finish what you asked: {goal}" if goal else "to carry on with what you asked"

        def send_link(url: str) -> None:
            minutes = f"{LOGIN_WAIT_S // 60:.0f} minutes"
            self.tars.post(chat, f"To go on I need to sign in to {site}, and I don't have a login for it. Add it here "
                           f"(the link works once, for {minutes}):\n{url}")  # fmt: skip

        try:
            given = ask(Asking(site, why, tuple(fields)), send_link, host, port)
        except OSError as e:  # the port taken, or the address gone: said in the chat, and the errand told no
            print(f"(Telegram: couldn't serve the login page on {host}:{port}: {e})")
            self.tars.post(chat, f"I couldn't open the page to add a login for {site}, so I stopped there.", tries=1)
            return None
        self.tars.post(chat, "Saved. Signing in." if given else f"No login for {site} came in time, so I stopped.")
        return given

    def code(self, person: str, site: str, answer: Answer) -> str | None:
        """The code `site` sent `person`: what they send back in the chat, or what their email has first."""
        chat = self.tars.chat_for(person)
        if chat is None:
            return answer.wait(CODE_WAIT_S) if answer.mail else None
        also = " I'm looking for it in your email too." if answer.mail else ""
        self._codes[chat.chat_id] = answer
        try:
            self.tars.post(chat, f"{site} has sent you a sign-in code. Send it here.{also}")
            code = answer.wait(CODE_WAIT_S)
        finally:
            self._codes.pop(chat.chat_id, None)
            answer.done.set()
        if answer.came_from == "email":
            self.tars.post(chat, "Found it in your email. Signing in.", tries=1)
        elif code is None:
            self.tars.post(chat, "No code came in time, so I stopped signing in.", tries=1)
        return code

    def _take(self, update: dict) -> bool:
        """Whether `update` is this one's: a tap on one of its buttons, or a code a sign-in waits for."""
        if tap := update.get("callback_query"):
            self._tapped(tap)
            return True
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        waiting = self._codes.get(chat_id)
        if waiting is None or (message.get("chat") or {}).get("type") != "private":
            return False
        code = one_time_code(message.get("text") or "")
        if code is None:
            return False  # not a code: it's for the conversation, as ever
        if waiting.give(code, "chat"):
            chat = self.tars.chat_for_id(chat_id)
            if chat is not None:
                self.tars.post(chat, "Got it. Signing in.", tries=1)
        return True

    def _tapped(self, tap: dict) -> None:
        """A tap on Order or Cancel: counted only on the message it was sent with, from the chat it was sent to."""
        kind, _, nonce = str(tap.get("data", "")).partition(":")
        waiting = self._taps.get(nonce)
        try:
            self.tars.bot.answer_tap(tap["id"])
        except TelegramError, KeyError:
            pass  # only the button's spinner
        if waiting is None or (tap.get("from") or {}).get("id") != waiting.chat_id or kind not in ("order", "cancel"):
            return
        if not waiting.tapped.is_set():
            waiting.answer = kind
            waiting.tapped.set()
