"""What TARS's browser agent may never do by itself: press the button that commits the person to something (places an
order, pays, books, reserves, sends). The agent is told so, and asks for approval instead (agent.py); this is what
holds when it doesn't listen, checked by code on every click and every key before it happens. It's code, not a
model's judgment, because the agent's model reads the pages it's on, and a page can say anything to it.

A label is matched by its words, in English and Hebrew, the languages of the sites TARS works. It errs on the side of
stopping: a "Continue to payment" stops the run at the step before payment, which is where an agent should stop
anyway, and a false stop costs a question, where a missed one costs money.
"""

# Words that commit wherever they are in a button's label.
ANYWHERE = [
    "place order", "place my order", "place your order", "place the order",
    "pay", "pay now", "buy", "buy now", "purchase", "book now", "order now", "reserve now",
    "confirm order", "confirm booking", "confirm reservation", "confirm purchase", "confirm payment",
    "confirm and pay", "confirm and book", "confirm and order", "confirm and reserve", "checkout and pay",
    "complete order", "complete booking", "complete reservation", "complete purchase", "complete payment",
    "submit order", "submit payment", "send order", "finish order", "request to book", "click to order", "click to pay",
    "continue to payment", "proceed to payment", "continue to pay", "proceed to pay",
    "בצע הזמנה", "ביצוע הזמנה", "אשר הזמנה", "אישור הזמנה", "סיום הזמנה", "שלח הזמנה", "השלם הזמנה", "השלמת הזמנה",
    "אשר רכישה", "אישור רכישה", "בצע רכישה", "ביצוע רכישה", "אשר תשלום", "אישור תשלום", "בצע תשלום", "ביצוע תשלום",
    "לתשלום", "תשלום", "שלם", "לרכישה", "רכישה", "קנה עכשיו", "הזמן עכשיו", "הזמינו עכשיו", "אישור ותשלום",
]  # fmt: skip
# Words that commit when they're the whole label, or the label with a price ("Order ₪112.40", "Reserve · €240"), the
# way the last button puts them; in a longer label ("Orders", "Book a table near you") they don't. "Book a table"
# isn't one: it opens a restaurant's booking form, which asks for the time and the people before anything is booked.
WHOLE = [
    "order", "book", "reserve", "confirm", "submit", "send",
    "book room", "book this room", "book stay", "book this stay", "book seats", "book tickets", "reserve seats",
    "הזמן", "הזמנה", "הזמינו", "אשר", "אישור", "שלח", "שליחה", "קנה", "קנו",
]  # fmt: skip
# Labels with those words that only show or start something: past orders, "Order again" (it fills a basket).
NEVER = [
    "order again", "reorder", "re order", "buy again", "buy it again", "view order", "view the order", "track",
    "my orders", "your orders", "order history", "order details", "payment method", "payment methods",
    "הזמן שוב", "הזמנה חוזרת", "ההזמנות שלי", "הזמנות קודמות", "היסטוריית", "אמצעי תשלום",
]  # fmt: skip
# Labels of buttons that only take the person on (to the basket or the checkout): the agent presses these itself.
MOVES_ON = [
    "go to checkout", "go to the checkout", "continue to checkout", "proceed to checkout", "checkout", "check out",
    "view basket", "view the basket", "view cart", "view the cart", "view bag", "open basket", "open cart",
    "show basket", "show cart", "go to basket", "go to cart", "basket", "cart", "show items",
    "לסל", "לקופה", "מעבר לקופה", "לסל הקניות", "לעגלת", "לעגלה", "לעגלת הקניות",
]  # fmt: skip


def words(label: str) -> list[str]:
    """A label's words, lower case, without punctuation, currency signs or numbers: "Place order · ₪112.40" is
    place, order."""
    plain = "".join(c if c.isalnum() else " " for c in label.lower())
    return [w for w in plain.split() if not w.isdigit()]


def _has(said: list[str], phrase: str) -> bool:
    want = phrase.split()
    return any(said[i : i + len(want)] == want for i in range(len(said) - len(want) + 1))


def commits(label: str) -> bool:
    """Whether a button or link with this label would commit the person to something."""
    said = words(label)
    if not said or any(_has(said, p) for p in NEVER):
        return False
    return said in [p.split() for p in WHOLE] or any(_has(said, p) for p in ANYWHERE)


def moves_on(label: str) -> bool:
    """Whether a button with this label only takes the person on, to the basket or the checkout."""
    said = words(label)
    return any(_has(said, p) for p in MOVES_ON) and not commits(label)
