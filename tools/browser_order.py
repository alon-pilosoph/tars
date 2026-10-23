"""Ordering, when TARS's browser agent is asked to buy something (a prototype): the agent may fill a basket and go to
checkout, but only code places an order, and only after the person approves the draft on Telegram. The draft is
read off the checkout page by code (not what the model says it ordered), and the order goes through only if the
total is still the one they approved. Without --really-order nothing is ever placed: the approval is asked for and
the button found, and that's where it stops."""

import re

from playwright.sync_api import Page

# A button that places an order or pays: what the agent may never press. "Order again" only fills a basket.
PLACE_ORDER = re.compile(
    r"\b(place|click to|confirm|submit|complete|send)\s+(the\s+|my\s+|your\s+)?order\b|^\s*order\s*(now|[·•]|₪|\d)"
    r"|\bpay\b|buy now|\bpurchase\b|checkout and pay",
    re.IGNORECASE,
)
NOT_PLACING = re.compile(r"order again|reorder|view order|track|my orders|your orders", re.IGNORECASE)
AMOUNT = re.compile(r"₪\s?(\d[\d,]*(?:\.\d{1,2})?)|(\d[\d,]*(?:\.\d{1,2})?)\s?₪")
DRAFT_CHARS = 1200


def places_order(label: str) -> bool:
    return bool(PLACE_ORDER.search(label)) and not NOT_PLACING.search(label)


def _visible_text(page: Page) -> str:
    """What a person sees of the checkout: the open dialog if there is one, else the page."""
    return page.evaluate("""() => {
      const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
      const dialogs = [...document.querySelectorAll('[aria-modal=true], dialog[open], [role=dialog]')].filter(shown);
      return (dialogs[dialogs.length - 1] || document.body).innerText;
    }""")


def total_of(page: Page) -> str | None:
    """The checkout's total, as the page shows it ("113.40"): the last amount on or just after a line saying total."""
    lines = [line.strip() for line in _visible_text(page).splitlines() if line.strip()]
    found = None
    for i, line in enumerate(lines):
        if "total" in line.lower():
            for candidate in (line, *lines[i + 1 : i + 3]):
                if amount := AMOUNT.search(candidate):
                    found = (amount.group(1) or amount.group(2)).replace(",", "")
                    break
    return found


def place_button(page: Page):
    """The one visible button that would place the order, or None if there isn't exactly one."""
    matches = [
        b for b in page.get_by_role("button").all()
        if b.is_visible() and places_order(b.inner_text(timeout=1000) or b.get_attribute("aria-label") or "")
    ]  # fmt: skip
    return matches[0] if len(matches) == 1 else None


def request_order(page: Page, person, goal: str, really: bool) -> str:
    """At checkout: the draft (read off the page) to the person on Telegram, and the order placed by code only if they
    approve and the total hasn't changed since. Without `really`, it stops at the button: nothing is ordered."""
    total = total_of(page)
    if total is None:
        return "no total on this page: get to the checkout page (the last step before ordering) first"
    button = place_button(page)
    if button is None:
        return "no single 'place order' button on this page: get to the final checkout step first"
    label = " ".join(button.inner_text(timeout=1000).split())
    draft = " ".join(_visible_text(page).split())[:DRAFT_CHARS]
    host = page.url.split("/")[2]
    approved = person.approve(
        f"TARS wants to order on {host}, for: {goal}\n\nTotal: ₪{total}\n\nThe checkout page says:\n{draft}"
        + ("" if really else "\n\n(Dry run: nothing will be ordered either way.)")
    )
    if not approved:
        return "the person didn't approve: nothing was ordered (call done and say so)"
    if total_of(page) != total:
        return f"the total changed from ₪{total} while waiting: nothing was ordered (call done and say so)"
    if not really:
        return f"dry run: approved; would have pressed {label!r} at ₪{total}. Nothing was ordered (call done)."
    button.click()
    page.wait_for_timeout(5000)
    return f"ordered: pressed {label!r} at ₪{total}; the page now says: {' '.join(_visible_text(page).split())[:400]}"
