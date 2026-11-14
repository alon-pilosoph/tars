"""Telling TARS's maintainers ([household] maintainers) something, in their Telegram chats, from whichever of TARS's
processes it is: as a message in the reminders table, which the Telegram service posts. What someone asked for and TARS
couldn't do (myself.py), and a device that's stopped answering (a plugin's watch, through home.Context.tell)."""

import time

from .config import Config


def tell(cfg: Config, store, text: str, by_chat: bool = False, besides: str | None = None) -> list[str]:
    """`text` to each maintainer with a Telegram chat, except `besides` (the one it's about, who knows). `by_chat`:
    it came from a chat rather than aloud. Who was told; nobody without [telegram]."""
    from .reminders import MESSAGE, TELEGRAM, VOICE, NewReminder, Reminders

    if not cfg.telegram.enabled:
        return []
    chats = {name.strip().lower(): name for name in cfg.telegram.people}
    me = (besides or "").strip().lower()
    told = []
    for maintainer in cfg.household.maintainers:
        name = chats.get(maintainer.strip().lower())
        if not name or name.lower() == me:
            continue
        try:
            Reminders(store).add(
                NewReminder(MESSAGE, text, for_name=name, due=time.time(), needs_ack=False, max_tries=1),
                set_via=TELEGRAM if by_chat else VOICE,
                channel=TELEGRAM,
                phones=[name],
            )
        except ValueError as e:
            print(f"(maintainers: couldn't tell {name}: {e})")
            continue
        told.append(name)
    return told
