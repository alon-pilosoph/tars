"""What TARS's browser agent may never do by itself: press what commits the person to something (places an order,
pays, books, reserves, sends). The agent is told so, and asks for approval instead (agent.py); this is what holds
when it doesn't listen, checked by code on every click and every key before it happens (session.py). It's code, not
a model's judgment, because the agent's model reads the pages it's on, and a page can say anything to it.

It goes by two things. What the button says, in English and Hebrew, the languages of the sites TARS works: the words
below, after the label is put in one form (Unicode's compatibility form, without the invisible marks right-to-left
text carries, without Hebrew's one-letter prefixes). And where it is (Press, from the page): whether its form or
dialog shows an amount ("Confirm" there is the last button; on a time picker it isn't), and whether the page takes
payment (card fields, a payment provider's frame), where any submit button commits, whatever it says.

It errs on the side of stopping: a false stop costs a question, a missed one costs money."""

import unicodedata
from dataclasses import dataclass

# Words that commit wherever they are in a button's label.
ANYWHERE = [
    "place order", "place my order", "place your order", "place the order", "place booking",
    "pay", "pay now", "buy", "buy now", "purchase", "book now", "order now", "reserve now", "donate", "subscribe",
    "start free trial", "start trial", "start subscription",
    "confirm order", "confirm booking", "confirm reservation", "confirm purchase", "confirm payment",
    "confirm and pay", "confirm and book", "confirm and order", "confirm and reserve", "checkout and pay",
    "complete order", "complete booking", "complete reservation", "complete purchase", "complete payment",
    "complete checkout", "finish checkout", "confirm checkout",
    "submit order", "submit payment", "send order", "finish order", "request to book", "click to order", "click to pay",
    "continue to payment", "proceed to payment", "continue to pay", "proceed to pay",
    "book this flight", "book flight", "book this hotel", "book this room", "book this stay", "book tickets",
    "בצע הזמנה", "בצעו הזמנה", "ביצוע הזמנה", "אשר הזמנה", "אשרו הזמנה", "אישור הזמנה", "סיום הזמנה", "סיימו הזמנה",
    "שלח הזמנה", "שלחו הזמנה", "שליחת הזמנה", "השלם הזמנה", "השלמת הזמנה", "הזמן עכשיו", "הזמינו עכשיו",
    "אשר רכישה", "אישור רכישה", "בצע רכישה", "ביצוע רכישה", "השלמת רכישה", "רכוש עכשיו", "רכשו עכשיו", "קנה עכשיו",
    "קנו עכשיו", "אשר תשלום", "אישור תשלום", "בצע תשלום", "ביצוע תשלום", "אישור ותשלום",
    "תשלום", "שלם", "שלמו", "רכישה", "לרכישה",
]  # fmt: skip
# A label with one of these and one of those commits, whatever's between them: "Confirm the scheduled order",
# "Complete your booking", "השלם את ההזמנה".
VERBS = ["confirm", "complete", "submit", "place", "finish", "send", "אשר", "אשרו", "אישור", "בצע", "בצעו", "השלם",
         "השלימו", "סיים", "סיימו", "שלח", "שלחו"]  # fmt: skip
NOUNS = ["order", "booking", "reservation", "purchase", "payment", "הזמנה", "רכישה", "תשלום", "עסקה"]
# Words that commit when they're the whole label, with nothing but an amount, a count or a "for" beside them ("Order
# ₪112.40", "Book for €240", "Reserve for 2", "הזמן ב-₪45"); in a longer label ("Orders", "Book a table near you")
# they don't. "Book a table" isn't one: it opens a restaurant's booking form, which asks the time and the people.
WHOLE = [
    "order", "book", "reserve", "book room", "book stay", "book seats", "reserve seats",
    "הזמן", "הזמנה", "הזמינו", "קנה", "קנו",
]  # fmt: skip
# Words that commit as the whole label only where the button's form or dialog shows an amount: "Confirm" there is a
# checkout's last button, and on a time picker or an address it isn't.
PRICED = ["confirm", "submit", "send", "done", "אשר", "אשרו", "אישור", "שלח", "שלחו", "שליחה", "סיום"]
# Words of a label that don't change what it is: amounts' and counts' words, beside the price.
FILLER = {"for", "at", "of", "x", "item", "items", "nis", "ils", "usd", "eur", "gbp", "ב", "ש", "ח", "שח", "פריט",
          "פריטים"}  # fmt: skip
# Labels, or the part of one, that only show or start something: past orders, "Order again" (it fills a basket),
# the payment methods kept on an account. What else the label says still counts.
NEVER = [
    "order again", "reorder", "re order", "buy again", "buy it again", "view order", "view the order", "track",
    "track order", "my orders", "your orders", "order history", "order details", "payment method", "payment methods",
    "הזמן שוב", "הזמנה חוזרת", "ההזמנות שלי", "הזמנות קודמות", "היסטוריית הזמנות", "פרטי הזמנה", "אמצעי תשלום",
]  # fmt: skip
# Labels of buttons that only take the person on (to the basket or the checkout): the agent presses these itself, but
# not on a page that takes payment.
MOVES_ON = [
    "go to checkout", "go to the checkout", "continue to checkout", "proceed to checkout", "checkout", "check out",
    "view basket", "view the basket", "view cart", "view the cart", "view bag", "open basket", "open cart",
    "show basket", "show cart", "go to basket", "go to cart", "basket", "cart", "show items",
    "לסל", "לקופה", "מעבר לקופה", "לסל הקניות", "לעגלת", "לעגלה", "לעגלת הקניות", "מעבר לעגלת הקניות", "הצגת פריטים",
]  # fmt: skip
HEBREW_PREFIXES = "ובלהשמכ"  # one-letter prefixes Hebrew writes onto a word: ולתשלום is ו + לתשלום


@dataclass(frozen=True)
class Press:
    """What pressing something would press, as the page shows it (session.py): every label it goes by (its own text,
    the button or clickable area it's in, what a label points at), whether its form or dialog shows an amount,
    whether the page takes payment, and whether it submits a form."""

    labels: tuple[str, ...]
    priced: bool = False
    paying: bool = False
    submits: bool = False


def words(label: str) -> list[str]:
    """A label's words: in one Unicode form, lower case, without invisible marks, punctuation, currency signs or
    numbers. "Place order · ₪112.40" is place, order."""
    label = unicodedata.normalize("NFKC", label)
    plain = "".join(c for c in label if unicodedata.category(c) != "Cf")  # direction marks, soft hyphens
    plain = "".join(c if c.isalnum() else " " for c in plain.lower())
    return [w for w in plain.split() if not w.isdigit()]


def _forms(word: str) -> set[str]:
    """A word, and without its Hebrew one-letter prefixes (up to two): ולתשלום is also לתשלום and תשלום."""
    found = {word}
    for _ in range(2):
        found |= {w[1:] for w in found if len(w) > 3 and w[0] in HEBREW_PREFIXES}
    return found


def _at(said: list[str], phrase: str) -> list[int]:
    """Where `phrase` starts in `said`, any of its words in any of their forms."""
    want = phrase.split()
    return [i for i in range(len(said) - len(want) + 1)
            if all(w in _forms(said[i + j]) for j, w in enumerate(want))]  # fmt: skip


def _without(said: list[str], phrases: list[str]) -> list[str]:
    """`said`, with the words of each of `phrases` it has taken out."""
    gone: set[int] = set()
    for phrase in phrases:
        for i in _at(said, phrase):
            gone |= set(range(i, i + len(phrase.split())))
    return [w for i, w in enumerate(said) if i not in gone]


def _whole(said: list[str], phrases: list[str]) -> bool:
    core = [w for w in said if not _forms(w) & FILLER]
    return any(len(core) == len(p.split()) and _at(core, p) == [0] for p in phrases)


def says_commit(label: str, priced: bool = False) -> bool:
    """Whether a button with this label commits, by its words alone: `priced`, it's where an amount is shown."""
    said = _without(words(label), NEVER)
    if not said:
        return False
    verb_and_noun = any(_at(said, v) for v in VERBS) and any(_at(said, n) for n in NOUNS)
    return (
        verb_and_noun or any(_at(said, p) for p in ANYWHERE) or _whole(said, WHOLE) or (priced and _whole(said, PRICED))
    )


def commits(press: Press | str) -> bool:
    """Whether pressing it would commit the person to something: what it says, where it is, or a submit button on a
    page that takes payment."""
    if isinstance(press, str):
        press = Press((press,))
    if press.paying and press.submits:
        return True
    return any(says_commit(label, press.priced) for label in press.labels)


def moves_on(label: str, paying: bool = False) -> bool:
    """Whether a button with this label only takes the person on, to the basket or the checkout: never on a page that
    takes payment."""
    said = words(label)
    return not paying and any(_at(said, p) for p in MOVES_ON) and not says_commit(label)
