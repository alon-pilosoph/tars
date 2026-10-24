"""Amounts of money as a page writes them, read the same way however it writes them: ₪112.40, 112.40 ₪, 112.40 ש"ח,
₪ 1,234.50, 1.234,50 €, USD 12, with the invisible marks right-to-left text puts around numbers. Used to show the
person the amounts beside what they're asked to approve, and to check they're the same when it's pressed."""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

# How each currency is written, and the one it's said as.
CURRENCIES = {
    "₪": "₪", "ils": "₪", "nis": "₪", 'ש"ח': "₪", "ש״ח": "₪", "שח": "₪",
    "$": "$", "usd": "$", "€": "€", "eur": "€", "£": "£", "gbp": "£", "¥": "¥", "jpy": "¥", "₹": "₹", "inr": "₹",
    "chf": "CHF", "zł": "zł", "pln": "zł",
}  # fmt: skip
NUMBER = re.compile(r"\d[\d.,']*\d|\d")  # 1,234.50 · 1.234,50 · 1'234.50 · 12
NEAR = 4  # characters between an amount and its currency, at most: "₪ 45", "45.00 ₪"


def _plain(text: str) -> str:
    # A no-break space between thousands (1\u00a0234,50) is a separator: kept as one, since NFKC makes it a space.
    text = unicodedata.normalize("NFKC", text.replace("\u00a0", "'").replace("\u202f", "'"))
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def _value(digits: str) -> Decimal | None:
    """1,234.50 and 1.234,50 and 1 234,50 are 1234.50; 1,234 is 1234; 12,50 is 12.50."""
    digits = digits.replace(" ", "").replace(" ", "").replace(" ", "").replace("'", "")
    dot, comma = digits.rfind("."), digits.rfind(",")
    point = max(dot, comma)
    if point >= 0 and (dot >= 0) == (comma >= 0) or (point >= 0 and len(digits) - point - 1 != 3):
        whole, cents = digits[:point], digits[point + 1 :]  # the last separator is the decimal point
    else:
        whole, cents = digits, ""  # one kind of separator, three digits after: thousands
    whole = whole.replace(".", "").replace(",", "")
    try:
        return Decimal(f"{whole}.{cents}" if cents else whole)
    except InvalidOperation:
        return None


def _currency(beside: str, before: bool) -> str | None:
    """The currency written right before (or after) an amount, within NEAR characters."""
    beside = beside.lower()
    window = beside[-(NEAR + 3) :] if before else beside[: NEAR + 3]
    for written, said in sorted(CURRENCIES.items(), key=lambda c: -len(c[0])):
        at = window.rfind(written) if before else window.find(written)
        if at < 0:
            continue
        gap = window[at + len(written) :] if before else window[:at]
        if gap.strip(" '") or len(gap) > NEAR:  # ' is what a no-break space became (_plain)
            continue
        if not before and beside[at + len(written) :].lstrip()[:1].isdigit():
            continue  # "2 ₪10": the ₪ is the 10's, not the 2's
        return said
    return None


def amounts(text: str) -> list[str]:
    """The amounts in `text`, in order, each as "₪112.40" (two decimals), whatever way they're written."""
    text = _plain(text)
    found = []
    for m in NUMBER.finditer(text):
        currency = _currency(text[: m.start()], before=True) or _currency(text[m.end() :], before=False)
        if currency is None:
            continue
        value = _value(m.group().strip(" ,.'"))
        if value is not None:
            found.append(f"{currency}{value:.2f}")
    return found
