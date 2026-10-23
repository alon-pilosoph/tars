"""What TARS's browser agent may never do by itself: press the button that commits the person to something (places an
order, pays, books, reserves, sends). The agent is told so, and asks for approval instead (agent.py); this is what
holds when it doesn't listen, checked by code on every click and every Enter before it happens.

A label is matched in English and Hebrew, the languages of the sites TARS works. It errs on the side of stopping: a
"Continue to payment" stops the run at the step before payment, which is where an agent should stop anyway, and a
false stop costs a question, where a missed one costs money.
"""

import re

# The words of a button that commits. Kept to what a commit button says, so that "Orders" or "Book a table" in a menu
# isn't one; "order", "book" and "reserve" on their own count only as the whole label, or with a price after them
# ("Order ₪112.40", "Reserve · €240"), the way checkout buttons put them.
_EN = (
    r"\b(place|click to|confirm|submit|complete|send|finish)\s+(the\s+|my\s+|your\s+|this\s+)?"
    r"(order|booking|reservation|purchase|payment)\b"
    r"|\b(pay|buy|book|order|reserve)\s+now\b"
    r"|\bpay\b|\bpurchase\b|checkout and pay|confirm and pay|request to book|\bcontinue to payment\b"
    r"|^\s*(order|book|reserve)\s*([·•:|-]|[₪$€£]|\d|$)"
)
_HE = (
    r"\b(בצע|ביצוע|אשר|אישור|סיום|שלח|השלם|השלמת)\s+(ה)?(הזמנה|רכישה|תשלום|עסקה)\b"
    r"|\b(לתשלום|תשלום|שלם|לרכישה|רכישה|קנה עכשיו|הזמן עכשיו|הזמינו עכשיו|אישור ותשלום)\b"
    r"|^\s*(הזמן|הזמנה|הזמינו)\s*([·•:|-]|[₪$€£]|\d|$)"
)
COMMITS = re.compile(f"{_EN}|{_HE}", re.IGNORECASE)
# Buttons with those words that only show or start something: past orders, "Order again" (it fills a basket).
NOT_COMMITTING = re.compile(
    r"order again|reorder|re-order|view (the\s+)?order|track|my orders|your orders|order history|order details"
    r"|payment methods?\b|הזמן שוב|הזמנה חוזרת|ההזמנות שלי|הזמנות קודמות|היסטוריית|אמצעי תשלום",
    re.IGNORECASE,
)


def commits(label: str) -> bool:
    """Whether a button or link with this label would commit the person to something."""
    label = " ".join(label.split())
    return bool(COMMITS.search(label)) and not NOT_COMMITTING.search(label)
