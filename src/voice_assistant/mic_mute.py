"""The home tool's "TARS's microphone", for a chat: whether TARS listens at home, muted and unmuted from afar.

The assistant holds the speakerphone, so the chat's process asks it over control.py (`{"op": "mute"}`), and it mutes
or unmutes the way the speakerphone's own button does (speakerphone.py): the mic and its light, or, without one, TARS
alone stops listening. What's said is what the assistant answers it is now, so it's "done", never assumed.
"""

from pathlib import Path

from . import control
from .home import OFF, ON, STATUS, Check, Device, HomeError, Request

DEVICE = "TARS's microphone"
OP = "mute"  # control.py's: {"muted": true or false} sets it; without, only says


class MicMute(Device):
    """The home tool's "TARS's microphone": on is listening, off is muted, asked of the assistant over control.py."""

    actions = (STATUS, ON, OFF)

    def __init__(self, folder: Path, ask=control.ask):
        """`folder`: the event log's, where the assistant's socket is."""
        super().__init__(DEVICE)
        self._ask = lambda **args: ask(folder, OP, **args)

    def what(self) -> str:
        return "your microphone at home, whether you listen there: status; on unmutes it, off mutes it"

    def rules(self) -> str:
        return (
            f'"Mute yourself", "unmute yourself" and "are you listening?" in a chat are about {DEVICE}, never your '
            "speaker: in a chat, that replaces what TARS's speaker's rules say about muting. Unmuted, the microphone "
            "hears the room at home again; muted, you don't listen there until someone unmutes you (its button, the "
            "remote, or a chat)."
        )

    def status(self) -> str:
        return f"{DEVICE}: {said(self._answer())}"

    def glance(self) -> str:
        return "muted" if self._answer() else "listening"

    def call(self, action: str, args: dict, request: Request) -> str:
        want = action == OFF
        muted = self._answer(muted=want)
        if muted != want:
            return f"not confirmed: TARS was asked to {'mute' if want else 'unmute'}, and is still {said(muted)}"
        return f"done: {DEVICE} is {said(muted)}"

    def check(self) -> list[Check]:
        try:
            return [Check("ok", said(self._answer()))]
        except HomeError as e:
            return [Check("warn", str(e), "is the voice assistant running? (systemctl --user status voice-assistant)")]

    def _answer(self, **args) -> bool:
        try:
            return bool(self._ask(**args)["muted"])
        except control.Unavailable:
            raise HomeError("the voice assistant at home isn't running, so there's no microphone to mute") from None
        except control.Refused as e:
            if e.why == "unknown":  # an assistant without a mute of its own to work (not Linux), or an older one
                raise HomeError("the voice assistant can't be muted from here") from None
            raise HomeError(f"the voice assistant said no ({e.why})") from None


def said(muted: bool) -> str:
    return "muted: not listening at home" if muted else "on: listening at home"
