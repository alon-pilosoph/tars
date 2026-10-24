"""What a one-time sign-in code is, wherever it comes from (sent back in a chat, read from an email): 4 to 10 plain
letters and digits, at least one a digit, as sites send them ("482913", "G-482913", "AB12 CD"); written with spaces
or dashes, or in another script's digits (٤٨٢٩١٣), it's the same code."""

import unicodedata

SHORTEST, LONGEST = 4, 10


def one_time_code(text: str) -> str | None:
    """The code `text` is, on its own; None if it isn't one ("1500 shekels", "hello")."""
    plain = "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in text.strip())
    plain = "".join(plain.replace("-", " ").split())
    if not (SHORTEST <= len(plain) <= LONGEST and plain.isascii() and plain.isalnum()):
        return None
    return plain if any(c.isdigit() for c in plain) else None
