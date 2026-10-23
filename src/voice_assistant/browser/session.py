"""TARS's browser as the agent works it: what's on the page now (page.View), steps on what's in it by ref, and a
screenshot when words aren't enough.

Each step is done the way a person would do it, with Playwright's checks (the element is there, visible, enabled
and not covered) and real mouse and keyboard input; then the page is given time to settle before it's looked at
again. A link that opens a new tab moves the agent there, as it would a person. Every click and every Enter is
checked against the gate (gate.py) first: what would commit the person to something isn't done, whatever the model
says. What the page loaded behind the scenes (its JSON) is kept with the step, for the trace."""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import BrowserContext, Dialog, Page, Response
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import page as pages
from .gate import commits

# Resolves once the page has stopped changing for `quiet` ms (a pop-up finished sliding in, a list finished loading),
# or after `cap` ms whatever it's doing.
STILL_JS = """([quiet, most]) => new Promise((done) => {
  let timer = setTimeout(finish, quiet);
  const watch = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(finish, quiet); });
  const cap = setTimeout(finish, most);
  function finish() { watch.disconnect(); clearTimeout(cap); clearTimeout(timer); done(true); }
  watch.observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
})"""

# The ref of each thing to act on drawn on it, for a screenshot: what the picture shows is then what the view names.
MARKS_JS = """(marks) => {
  document.querySelectorAll('.tars-mark').forEach((m) => m.remove());
  for (const [ref, x, y] of marks) {
    const m = document.createElement('div');
    m.className = 'tars-mark';
    m.textContent = ref;
    m.style.cssText = `position:fixed;left:${x}px;top:${y}px;z-index:2147483647;background:#ffd400;color:#000;`
      + 'font:bold 11px/13px monospace;padding:0 2px;border:1px solid #000;pointer-events:none';
    document.documentElement.appendChild(m);
  }
}"""
# Which dialogs, suggestion lists and menus are open, as a signature.
SHAPE_JS = """() => [...document.querySelectorAll(
    '[role=dialog], [role=alertdialog], dialog[open], [role=listbox], [role=menu], [aria-expanded=true]')]
  .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; })
  .map((e) => (e.getAttribute('role') || e.tagName) + (e.getAttribute('aria-label') || '')).join(',')"""
UNMARK_JS = "() => document.querySelectorAll('.tars-mark').forEach((m) => m.remove())"

# What Enter would send, from the field it's pressed in: its form's submit button, as the gate reads labels.
SUBMITS_JS = """(e) => {
  const form = e.form || e.closest('form');
  if (!form) return '';
  const b = form.querySelector('[type=submit], button:not([type])');
  return b ? (b.innerText || b.value || b.getAttribute('aria-label') || '').trim() : '';
}"""

REF = re.compile(r"(f\d+)?e\d+")
KEYS = {"Enter", "Escape", "Tab", "Backspace", "Delete", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home",
        "End", "PageDown", "PageUp", "Space"}  # fmt: skip
JSON_KEPT = 20_000  # characters of a JSON response kept in the trace
JSON_LARGEST = 400_000  # bytes: anything larger is only noted


@dataclass
class Outcome:
    """What a batch of steps did, for the model: a line a step, stopping at the first that failed."""

    said: list[str] = field(default_factory=list)
    failed: bool = False
    refused: str = ""  # the label of what the gate stopped, if it did
    changed: bool = True

    def __str__(self) -> str:
        return "\n".join(self.said)


class Session:
    """One task's browser: a tab of TARS's Chrome, and what the agent has seen of it."""

    def __init__(self, context: BrowserContext, page: Page | None = None):
        self.context = context
        self.page = page or (context.pages[0] if context.pages else context.new_page())
        self.current: pages.View | None = None
        self.opened: list[Page] = []
        self.dialogs: list[str] = []
        self.network: list[Response] = []
        context.on("page", self._on_page)
        self._watch(self.page)

    def _watch(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("response", self._on_response)

    def _on_page(self, page: Page) -> None:
        self.opened.append(page)

    def _on_dialog(self, dialog: Dialog) -> None:
        """A page's own alert or confirm: an alert is read and closed; a confirm ("Are you sure?") is answered no,
        since saying yes is the person's to do. What it said goes to the model."""
        self.dialogs.append(f"the page showed a {dialog.type} saying {dialog.message!r}: answered no")
        try:
            if dialog.type == "alert":
                dialog.accept()
            else:
                dialog.dismiss()
        except PlaywrightError:
            pass

    def _on_response(self, response: Response) -> None:
        if response.request.resource_type in ("xhr", "fetch"):
            self.network.append(response)

    # Seeing

    def look(self) -> pages.View:
        """The page as it is now, as the model is shown it."""
        self._front()
        self._still()
        page = self.page
        try:
            snapshot = page.aria_snapshot(mode="ai", boxes=True, timeout=10_000)
        except PlaywrightError as e:  # mid-navigation: once more, after it lands
            self._settle()
            try:
                snapshot = page.aria_snapshot(mode="ai", boxes=True, timeout=10_000)
            except PlaywrightError:
                snapshot = f"- text: (the page couldn't be read: {str(e).splitlines()[0][:120]})"
        height = (page.viewport_size or {}).get("height") or page.evaluate("innerHeight")
        try:
            title = page.title()
        except PlaywrightError:
            title = ""
        self.current = pages.view(snapshot, title, page.url, int(height))
        return self.current

    def screenshot(self) -> bytes:
        """What's on screen, with the refs of what's on it drawn on: about 1,000 tokens at this size."""
        view = self.current or self.look()
        height = self.page.evaluate("innerHeight")
        width = self.page.evaluate("innerWidth")
        # Only what the view names: not what is behind an open dialog.
        named = set(re.findall(r"\[((?:f\d+)?e\d+) ", view.text))
        marks = [
            [ref, max(0, n.box[0]), max(0, n.box[1])]
            for ref, n in view.refs.items()
            if ref in named and n.box and 0 <= n.box[1] < height and n.box[0] < width and n.box[2] >= 8
        ]
        try:
            self.page.evaluate(MARKS_JS, marks)
            return self.page.screenshot(type="jpeg", quality=70, scale="css", timeout=10_000)
        finally:
            try:
                self.page.evaluate(UNMARK_JS)
            except PlaywrightError:
                pass

    # Acting

    def act(self, steps: list[dict[str, Any]]) -> Outcome:
        """Does each step in order, stopping at the first that fails or is refused; then lets the page settle."""
        outcome = Outcome()
        self._front()
        before = (self.page.url, [ln.text for ln in self.current.lines] if self.current else [])
        url = self.page.url
        for i, step in enumerate(steps):
            shape = self._shape()
            try:
                said = self._step(step)
            except Refused as e:
                outcome.said.append(f"refused: {e}")
                outcome.refused, outcome.failed = e.label, True
                break
            except (PlaywrightError, ValueError, KeyError, TypeError) as e:
                outcome.said.append(f"failed: {json.dumps(step, ensure_ascii=False)}: {_why(e)}")
                outcome.failed = True
                break
            outcome.said.append(f"ok: {said}")
            if self.page.url != url or "goto" in step or "back" in step:
                self._settle()  # a new page: until it has loaded
            else:
                self._still(quiet_ms=250, most_ms=1500)  # the same page: until what the step started has drawn
            url = self.page.url
            if self._follow_new_tab():
                outcome.said.append(f"it opened a new tab, now in front: {self.page.url}")
                shape = ""
            rest = len(steps) - i - 1
            if rest and self._shape() != shape:  # what the next steps were planned on has gone or moved
                outcome.said.append(
                    f"that opened or changed something (a new page, a dialog, a list of suggestions), so the "
                    f"{rest} step(s) after it weren't done: look at the page as it is now"
                )
                break
        outcome.said += self.dialogs
        self.dialogs.clear()
        after = self.look()
        outcome.changed = (after.url, [ln.text for ln in after.lines]) != before
        return outcome

    def _step(self, step: dict[str, Any]) -> str:
        page = self.page
        if "click" in step:
            ref = str(step["click"])
            target = self._target(ref)
            label = self._label(ref, target)
            if commits(label):
                raise Refused(label, f"{label!r} would commit the person to it: call request_approval with this ref")
            target.click(timeout=6_000)
            return f"clicked {ref} {label[:60]!r}"
        if "fill" in step and "type" not in step:  # what models also call it
            step = {**step, "type": step["fill"]}
        if "type" in step:
            ref = str(step["type"])
            field = self._target(ref)
            text = str(step.get("text", ""))
            if step.get("slowly"):
                field.click(timeout=6_000)
                field.press("ControlOrMeta+a")
                field.press_sequentially(text, delay=60, timeout=20_000)
            else:
                field.fill(text, timeout=6_000)
            if step.get("submit"):
                self._enter(field)
            return f"typed into {ref}" + (" and pressed Enter" if step.get("submit") else "")
        if "select" in step:
            ref = str(step["select"])
            chosen = self._target(ref).select_option(label=str(step.get("option", "")), timeout=6_000)
            return f"chose {chosen} in {ref}"
        if "press" in step:
            key = str(step["press"])
            if key not in KEYS:
                raise ValueError(f"no such key {key!r} (one of {', '.join(sorted(KEYS))})")
            if key == "Enter":
                self._enter(page.locator(":focus").first)
            else:
                page.keyboard.press(key)
            return f"pressed {key}"
        if "scroll" in step:
            where = str(step["scroll"])
            if where not in ("down", "up"):  # a ref: brought into view
                self._target(where).scroll_into_view_if_needed(timeout=6_000)
                return f"scrolled {where} into view"
            inside = step.get("in")
            box = self._target(str(inside)).bounding_box() if inside else None
            size = page.viewport_size or {"width": 1200, "height": 800}
            x, y = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2) if box else (
                size["width"] / 2, size["height"] / 2)  # fmt: skip
            page.mouse.move(x, y)
            page.mouse.wheel(0, (1 if where == "down" else -1) * size["height"] * 0.8)
            return f"scrolled {where}" + (f" in {inside}" if inside else "")
        if "hover" in step:
            ref = str(step["hover"])
            self._target(ref).hover(timeout=6_000)
            return f"hovered over {ref}"
        if "goto" in step:
            url = str(step["goto"])
            if not url.startswith(("https://", "http://")):
                raise ValueError("goto takes an http(s) address")
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            return f"went to {url}"
        if "back" in step:
            page.go_back(wait_until="domcontentloaded", timeout=20_000)
            return "went back"
        if "wait" in step:
            seconds = min(max(float(step["wait"]), 0.5), 10)
            page.wait_for_timeout(seconds * 1000)
            return f"waited {seconds:g}s"
        raise ValueError(f"unknown step {json.dumps(step, ensure_ascii=False)}")

    def _shape(self) -> str:
        """What a batch's later steps were planned on: the address, and which dialogs, lists of suggestions and menus
        are open. When it changes, the refs the model picked may be hidden, covered or gone."""
        try:
            return self.page.url + "|" + str(self.page.evaluate(SHAPE_JS))
        except PlaywrightError:
            return ""

    def _target(self, ref: str):
        if not REF.fullmatch(ref):
            raise ValueError(f"{ref!r} isn't a ref from the page")
        target = self.page.locator(f"aria-ref={ref}")
        if target.count() == 0:
            raise ValueError(f"{ref} isn't on the page any more: look at the page as it is now")
        target = target.first
        if not target.is_visible():  # once more after a moment: it may be sliding in
            self.page.wait_for_timeout(600)
            if not target.is_visible():
                raise ValueError(f"{ref} is there but hidden now (something closed or moved): look at the page")
        return target

    def _label(self, ref: str, target) -> str:
        """What the element says, for the gate: its name in the view, else its own text."""
        node = self.current.refs.get(ref) if self.current else None
        if node:
            return " ".join((node.name or pages.text_of(node)).split())
        try:
            return " ".join((target.inner_text(timeout=2_000) or target.get_attribute("aria-label") or "").split())
        except PlaywrightError:
            return ""

    def _enter(self, field) -> None:
        """Enter in a field: refused when what it would submit commits."""
        try:
            sends = field.evaluate(SUBMITS_JS)
        except PlaywrightError:
            sends = ""
        if sends and commits(sends):
            raise Refused(sends, f"Enter here would press {sends!r}, which commits: call request_approval instead")
        field.press("Enter")

    def click_approved(self, ref: str) -> None:
        """Clicks what the person approved: only ever called by code, after they said yes."""
        self._target(ref).click(timeout=10_000)
        self._settle()

    # Waiting

    def _still(self, quiet_ms: int = 400, most_ms: int = 3000) -> None:
        try:
            self.page.evaluate(STILL_JS, [quiet_ms, most_ms])
        except PlaywrightError:  # navigating away: what loads next is waited for instead
            self._settle()

    def _settle(self) -> None:
        """Until the page has loaded what it's going to, briefly: a page that keeps polling isn't waited out."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
            self.page.wait_for_load_state("networkidle", timeout=2_500)
        except PlaywrightTimeout, PlaywrightError:
            pass

    def _front(self) -> None:
        """If the tab it was on has closed (a pop-up that closes itself when done), the one still open."""
        if self.page.is_closed() and (open_pages := [p for p in self.context.pages if not p.is_closed()]):
            self.page = open_pages[-1]
            self._watch(self.page)

    def _follow_new_tab(self) -> bool:
        opened = [p for p in self.opened if not p.is_closed()]
        self.opened.clear()
        if not opened:
            return False
        self.page = opened[-1]
        self._watch(self.page)
        self.page.bring_to_front()
        self._settle()
        return True

    # The trace

    def take_network(self) -> list[dict[str, Any]]:
        """What the page fetched since the last call (its API calls), for the trace: JSON bodies kept, briefly."""
        taken, self.network = self.network, []
        out = []
        for r in taken[-40:]:
            try:
                out.append(_entry(r))
            except PlaywrightError, ValueError:  # gone with its page, or not text: the trace does without it
                continue
        return out


def _entry(r: Response) -> dict[str, Any]:
    entry: dict[str, Any] = {"method": r.request.method, "url": r.url[:500], "status": r.status}
    if r.request.method != "GET" and (sent := r.request.post_data_buffer):
        entry["sent"] = sent[:2_000].decode(errors="replace")
    if "json" in r.headers.get("content-type", ""):
        body = r.body()
        entry["json"] = body[:JSON_KEPT].decode(errors="replace") if len(body) <= JSON_LARGEST else f"({len(body)} B)"
    return entry


class Refused(Exception):
    def __init__(self, label: str, message: str):
        super().__init__(message)
        self.label = label


def _why(e: Exception) -> str:
    """What went wrong, briefly but with the reason: Playwright's first line is only "Timeout exceeded", and the
    lines after it say what's in the way."""
    lines = [line.strip() for line in str(e).splitlines() if line.strip()]
    if not lines:
        return type(e).__name__
    reasons = [ln for ln in lines[1:] if any(w in ln for w in ("intercepts", "not visible", "not enabled",
               "not stable", "outside of the viewport", "detached", "not an <input>", "did not find"))]  # fmt: skip
    return "; ".join([lines[0][:160], *[r[:160] for r in reasons[-2:]]])
