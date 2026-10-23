"""What TARS's browser agent sees of a page: the page's accessibility tree (what a screen reader reads), as Playwright
takes it for agents, made compact enough to send every turn.

Playwright's snapshot (`page.aria_snapshot(mode="ai", boxes=True)`) names every element by its role and accessible
name, with a ref that a click can use (`aria-ref=e36`), its state (checked, expanded, disabled) and where it is on
screen. It's what a person could perceive, from the browser itself, on any site: nothing here knows any one site.
But it's long (Wolt's front page is 400,000 characters), so it's rewritten as lines a model reads quickly:

- One line per block: a heading, a list item, a table row, a paragraph of the page's own. What's inside a block
  is written on its line, its text and the things to click or type into, the way a person reads a card.
- The things to act on carry their ref: `[e36 button "Home"]`. Nothing else does.
- Wrappers with no role of their own (most of the tree) disappear.

A page that's still too long is shown around what's on screen, with the headings above and below it, and `find`
searches all of it. An open dialog is what's in front of the person, so it's shown first and in full."""

import json
import re
from dataclasses import dataclass, field

# Roles a person acts on; anything with a pointer cursor counts too (sites make cards clickable in code).
ACTS = {
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch", "slider", "spinbutton",
    "option", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem", "gridcell", "columnheader",
}  # fmt: skip
# Roles that start a line of their own; the rest are written into the line they're in.
BLOCKS = {
    "heading", "listitem", "row", "paragraph", "dialog", "alertdialog", "alert", "status", "article", "form",
    "search", "navigation", "banner", "main", "contentinfo", "complementary", "region", "group", "list", "table",
    "tablist", "menu", "menubar", "listbox", "radiogroup", "tree", "grid", "blockquote", "figure", "caption",
    "definition", "term", "tabpanel", "toolbar", "iframe",
}  # fmt: skip
# Blocks that only hold other blocks: they get a line only when they have a name to say.
CONTAINERS = {
    "list", "table", "navigation", "banner", "main", "contentinfo", "complementary", "region", "group", "form",
    "search", "menu", "menubar", "tablist", "tree", "grid", "toolbar", "iframe", "tabpanel",
}  # fmt: skip
STATES = ("checked", "selected", "expanded", "pressed", "disabled", "active", "required", "invalid")
POPUPS = {"menu", "listbox"}  # what opens over a page from a button or a search box
POPUP_LINES = 40
LIMIT = 14_000  # characters of a view; a page longer than this is shown around the screen
LINE = 400  # characters of one line at most

_HEAD = re.compile(r"^(\s*)- (.*)$")
_ROLE = re.compile(r"([^\s:\"\[]*)(.*)$")
_ATTR = re.compile(r"\s*\[([^\]]*)\]")


@dataclass
class Node:
    role: str
    name: str = ""
    value: str = ""  # the text after the colon: a textbox's contents, a generic's text
    ref: str = ""
    box: tuple[int, int, int, int] | None = None
    states: list[str] = field(default_factory=list)
    level: str = ""
    pointer: bool = False
    url: str = ""
    placeholder: str = ""
    children: list[Node] = field(default_factory=list)

    @property
    def acts(self) -> bool:
        if not self.ref or (self.box and (self.box[2] < 1 or self.box[3] < 1)):
            return False
        return self.role in ACTS or self.pointer

    @property
    def shown(self) -> bool:
        """Whether a person could see it. Playwright leaves out what's hidden, so a box of no size is a wrapper that
        draws nothing itself (a fieldset, display: contents): it's shown when what's in it is."""
        if not self.box or (self.box[2] > 0 and self.box[3] > 0):
            return True
        return any(c.shown for c in self.children)

    @property
    def top(self) -> int | None:
        """Where it starts on screen: its own box, or its first child's for a wrapper of no size."""
        if self.box and (self.box[2] > 0 or self.box[3] > 0):
            return self.box[1]
        return next((t for c in self.children if (t := c.top) is not None), None)


def _unquote(text: str) -> str:
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    return text


def parse(snapshot: str) -> Node:
    """Playwright's snapshot (YAML: `- role "name" [attr]...: text`, children indented under it) as a tree."""
    root = Node("document")
    stack: list[tuple[int, Node]] = [(-1, root)]
    for line in snapshot.splitlines():
        found = _HEAD.match(line)
        if not found:
            continue
        indent, rest = len(found.group(1)), found.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if rest.startswith("/"):  # a property of the element above: /url, /placeholder
            key, _, value = rest.partition(":")
            if key == "/url":
                parent.url = _unquote(value)
            elif key == "/placeholder":
                parent.placeholder = _unquote(value)
            continue
        node = _node(rest)
        parent.children.append(node)
        stack.append((indent, node))
    return root


def _node(rest: str) -> Node:
    found = _ROLE.match(rest)
    node = Node(found.group(1))
    rest = found.group(2)
    rest = rest.strip()
    if rest.startswith('"'):  # the accessible name, a JSON-style string
        end, escaped = 1, False
        while end < len(rest):
            if rest[end] == "\\" and not escaped:
                escaped = True
            elif rest[end] == '"' and not escaped:
                break
            else:
                escaped = False
            end += 1
        node.name, rest = _unquote(rest[: end + 1]), rest[end + 1 :]
    while found := _ATTR.match(rest):
        attr, rest = found.group(1), rest[found.end() :]
        key, _, value = attr.partition("=")
        if key == "ref":
            node.ref = value
        elif key == "box":
            try:
                x, y, w, h = (int(float(v)) for v in value.split(","))
                node.box = (x, y, w, h)
            except ValueError:
                pass
        elif key == "cursor":
            node.pointer = value == "pointer"
        elif key == "level":
            node.level = value
        elif key in STATES and value in ("", "true", "mixed"):
            node.states.append(key if value != "mixed" else f"{key}=mixed")
    rest = rest.strip()
    if rest.startswith(":"):
        node.value = _unquote(rest[1:])
    return node


@dataclass
class Line:
    depth: int
    text: str
    top: int | None  # where it is on screen (CSS pixels from the top of the viewport), when known
    heading: bool = False


def _label(node: Node) -> str:
    """How an element to act on is written: its ref, its role, its name, its state and what's in it."""
    name = node.name or text_of(node) or node.placeholder
    parts = [node.ref, node.role if node.role in ACTS else "clickable"]
    if name:
        parts.append(json.dumps(" ".join(name.split())[:120], ensure_ascii=False))
    if node.role in ("textbox", "searchbox", "combobox", "spinbutton") and node.value:
        parts.append(f"= {json.dumps(node.value[:80], ensure_ascii=False)}")
    elif node.placeholder and node.name and node.role in ("textbox", "searchbox"):
        parts.append(f"(placeholder {json.dumps(node.placeholder[:40], ensure_ascii=False)})")
    parts += node.states
    if node.role == "link" and node.url and not node.name:
        parts.append(node.url[:80])
    return "[" + " ".join(parts) + "]"


def text_of(node: Node) -> str:
    """The text inside an element, as one string (for an element to act on that has no name of its own)."""
    out: list[str] = []

    def walk(n: Node) -> None:
        if n.name and n.role not in ("generic", "text", "paragraph"):
            out.append(n.name)
        elif n.value and n.role in ("generic", "text", "paragraph", "strong", "emphasis", "cell"):
            out.append(n.value)
        else:
            for c in n.children:
                walk(c)

    for c in node.children:
        walk(c)
    return " ".join(" ".join(out).split())


def lines(root: Node) -> list[Line]:
    """The page as lines: a block a line, what's in it written on it."""
    writer = _Writer()
    for c in root.children:
        if c.role in BLOCKS:
            writer.block(c, 0)
        else:
            writer.loose(c)
    return writer.out


class _Writer:
    """Writes a tree out as lines. A block starts a line of its own at one level deeper than the block it's in;
    everything else inside it is written into the line being made (`parts`), which ends at the next block."""

    def __init__(self) -> None:
        self.out: list[Line] = []
        self.depth = 0
        self.top: int | None = None  # where the block being written starts on screen

    def block(self, node: Node, depth: int) -> None:
        if not node.shown:
            return
        saved = self.depth, self.top
        self.depth, self.top = depth, node.top if node.top is not None else self.top
        if node.role == "heading":
            if said := " ".join((node.name or text_of(node)).split()):
                heading = ("#" * int(node.level or 2) + " " + said)[:LINE]
                self.out.append(Line(depth, heading, self.top, heading=True))
            for c in node.children:  # a link or a button in a heading: its text is the heading's, said already
                if _has_act(c):
                    parts: list[str] = []
                    self.inline(c, parts)
                    self.flush(parts)
        elif node.role in CONTAINERS and not node.name:  # a wrapper that says nothing: its blocks at its level
            for c in node.children:
                if c.role in BLOCKS:
                    self.block(c, depth)
                else:
                    self.loose(c)
        else:
            parts = [_opening(node)]
            if node.name and node.role not in PLAIN and node.role not in CONTAINERS:
                parts.append(node.name)
            if node.value:
                parts.append(node.value)
            for c in node.children:
                if c.role in BLOCKS:
                    self.flush(parts)
                    self.block(c, depth + 1)
                else:
                    self.inline(c, parts)
            self.flush(parts)
        self.depth, self.top = saved

    def loose(self, node: Node) -> None:
        """Something not in a block of its own (most of a page, in wrappers): a line of whatever it holds."""
        saved = self.top
        self.top = node.top if node.top is not None else saved
        parts: list[str] = []
        self.inline(node, parts)
        self.flush(parts)
        self.top = saved

    def inline(self, node: Node, parts: list[str]) -> None:
        """Writes `node` into the line being made, and any blocks inside it as lines of their own."""
        if not node.shown:
            return
        if node.acts and not (node.role not in ACTS and _has_real_act(node)):
            parts.append(_label(node))
            for c in node.children:  # an element inside it that acts on its own: a button in a clickable card
                if _has_act(c):
                    self.inline(c, parts)
            return
        if node.role in ("img", "image") and node.name:
            parts.append(f"(image: {node.name[:60]})" if len(node.name) > 3 else node.name)
        elif node.name and node.role not in ("generic", "text", "none", "presentation"):
            parts.append(node.name)
        if node.value and node.role not in ("textbox", "searchbox", "combobox"):
            parts.append(node.value)
        for c in node.children:
            if c.role in BLOCKS:
                self.flush(parts)
                self.block(c, self.depth + 1)
            else:
                self.inline(c, parts)

    def flush(self, parts: list[str]) -> None:
        """Ends the line being made: in pieces of at most LINE characters, broken between parts, so nothing is lost."""
        piece, last = "", ""
        for part in (p.strip() for p in parts):
            if not part or (not part.startswith("[") and part in last):  # a label's own text, said again beside it
                continue
            last = part
            if piece and len(piece) + 1 + len(part) > LINE:
                self.out.append(Line(self.depth, piece, self.top))
                piece = ""
            piece = f"{piece} {part}" if piece else part[: LINE * 2]
        if piece:
            self.out.append(Line(self.depth, piece, self.top))
        parts.clear()


# Blocks whose name isn't said on their line: it's the text in them, written out anyway, or said in the opening.
PLAIN = ("paragraph", "listitem", "row", "dialog", "alertdialog", "alert", "status")


def _opening(node: Node) -> str:
    """How a block's line starts: what it is, when that's worth saying."""
    name = json.dumps(node.name, ensure_ascii=False)
    if node.role in ("dialog", "alertdialog"):
        return f"{node.role} {name}:" if node.name else "dialog:"
    if node.role in CONTAINERS or node.role in ("alert", "status"):
        return f"{node.role} {name}:"
    if node.role in ("listitem", "row", "option"):
        return "-"
    return ""


def _has_real_act(node: Node) -> bool:
    """Whether a real button or link is inside: then a clickable card around it is that button, not one of its own
    (sites make the whole card clickable and put the button in it too)."""
    return any((c.acts and c.role in ACTS) or _has_real_act(c) for c in node.children)


def _has_act(node: Node) -> bool:
    return node.acts or any(_has_act(c) for c in node.children)


def _find_dialog(node: Node) -> Node | None:
    """The open dialog in front, if any: the last one shown, as it's the one on top."""
    found = None
    for c in node.children:
        if c.role in ("dialog", "alertdialog") and c.shown and _has_act(c):
            found = c
        found = _find_dialog(c) or found
    return found


@dataclass
class View:
    """A page as the agent is shown it, and all of it for `find`."""

    title: str
    url: str
    text: str  # what's sent: all of it, or what's around the screen
    lines: list[Line]  # all of it
    refs: dict[str, Node]  # the elements to act on, by ref
    dialog: bool
    whole: bool  # whether `text` is the whole page
    popups: frozenset[str] = frozenset()  # the menus and lists of options open on it, by ref

    def find(self, query: str, limit: int = 30) -> str:
        """Lines anywhere on the page with all of the query's words in them (any case)."""
        words = [w.casefold() for w in query.split() if w.strip()]
        if not words:
            return "find what? give some words to look for"
        hits = [ln for ln in self.lines if all(w in ln.text.casefold() for w in words)]
        if not hits:
            return f"nothing on this page has {query!r} in it"
        shown = "\n".join(_where(ln) + ln.text for ln in hits[:limit])
        more = f"\n(and {len(hits) - limit} more)" if len(hits) > limit else ""
        return f"{len(hits)} line(s) with {query!r}:\n{shown}{more}"


def _where(line: Line) -> str:
    return "" if line.top is None else ("(above) " if line.top < 0 else "")


def view(snapshot: str, title: str, url: str, height: int, popups_before: frozenset[str] = frozenset()) -> View:
    """The page from Playwright's snapshot, as the agent sees it: all of it if it's short; else around the screen
    (from a quarter of a screen above it to a screen and a half below), with the headings outside that, so the agent
    knows what's there to scroll to or find. An open dialog first, and the page behind it only in outline. A menu or
    a list of options that wasn't open before (not in `popups_before`) first of all: sites draw them at the end of the
    page, far from the button that opened them, and they're what the person is looking at."""
    root = parse(snapshot)
    refs: dict[str, Node] = {}
    popups: list[Node] = []

    def collect(n: Node) -> None:
        if n.acts:
            refs[n.ref] = n
        if n.role in POPUPS and n.ref and n.shown and _has_act(n):
            popups.append(n)
        for c in n.children:
            collect(c)

    collect(root)
    every = lines(root)
    head = [f"Title: {title}", f"Address: {url}"]
    opened = [p for p in popups if p.ref not in popups_before]
    if opened:
        shown = [_indent(ln) for p in opened for ln in lines(Node("document", children=[p]))][:POPUP_LINES]
        head += ["", "Just opened, in front of the page:", *shown]
    open_now = frozenset(p.ref for p in popups)
    dialog = _find_dialog(root)
    if dialog:
        inside = lines(Node("document", children=[dialog]))
        behind = [ln for ln in every if ln.heading and ln.text.strip("# ")]
        body = (
            ["A dialog is open in front of the page: it's what can be used now (close it to get back to the page)."]
            + [_indent(ln) for ln in inside]
            + ["", "The page behind it, in outline:"]
            + [_indent(ln) for ln in behind[:30]]
        )
        text, whole = "\n".join(head + [""] + body), False
        if len(text) > LIMIT:
            text = text[:LIMIT] + "\n(the dialog goes on: scroll in it, or find)"
        return View(title, url, text, every, refs, True, whole, open_now)
    full = "\n".join(head + [""] + [_indent(ln) for ln in every])
    if len(full) <= LIMIT:
        return View(title, url, full, every, refs, False, True, open_now)
    top, bottom = -height // 4, height + height * 3 // 2
    near = [i for i, ln in enumerate(every) if ln.top is not None and top <= ln.top <= bottom]
    if not near:  # nothing placed on screen (a page without boxes): its beginning
        near = list(range(min(len(every), 120)))
    first, last = near[0], near[-1]
    above = [ln for ln in every[:first] if ln.heading and ln.text.strip("# ")]
    below = [ln for ln in every[last + 1 :] if ln.heading and ln.text.strip("# ")]
    shown = [_indent(ln) for ln in every[first : last + 1]]
    body = []
    if first:
        body += [f"Above the screen ({first} lines; scroll up or find to reach them), headings:"]
        body += ["  " + ln.text for ln in above[-15:]] or ["  (none)"]
        body += ["", "On screen and just below it:"]
    body += shown
    if last + 1 < len(every):
        body += ["", f"Further down ({len(every) - last - 1} lines; scroll down or find to reach them), headings:"]
        body += ["  " + ln.text for ln in below[:25]] or ["  (none)"]
    text = "\n".join(head + [""] + body)
    if len(text) > LIMIT:
        text = text[:LIMIT] + "\n(cut short: scroll or find for the rest)"
    return View(title, url, text, every, refs, False, False, open_now)


def _indent(line: Line) -> str:
    return "  " * min(line.depth, 6) + line.text
