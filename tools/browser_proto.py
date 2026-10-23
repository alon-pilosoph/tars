"""A prototype of TARS working a browser for someone: real Chrome with TARS's own profile (signed in once, by hand),
a compact view of each page, and a model that answers with a batch of actions at a time. Not part of TARS yet.

    uv run --all-extras python tools/browser_proto.py signin [URL]     # a visible Chrome: sign in by hand, quit it
    uv run --all-extras python tools/browser_proto.py run "GOAL" [--model qwen|luna] [--steps N] [--headless]
    uv run --all-extras python tools/browser_proto.py view [URL]       # print the view a model gets, no model

Every command starts its own Chrome and shuts it at the end: nothing is borrowed from a window left open, only what
TARS's profile keeps on disk. It's a real window, not headless: headless Chrome gives itself away (it says
HeadlessChrome, and its screen is smaller than its window), and Amazon answers it with an "I am human" check. On the
Pi it would draw to a virtual screen (Xvfb) instead, which is also what a "take over" view would show. Chrome is
started without automation flags (sites refuse sign-in to browsers that say they're automated) and with a debugging
port, and Playwright attaches to it. The profile is ~/.cache/tars-browser/profile: everything signed into there stays
signed in, so keep it to this machine."""

import argparse
import json
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from browser_order import places_order, request_order
from browser_signin import Person, sign_in, tailscale_address
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PageTimeout

from voice_assistant.__main__ import make_openai_client, make_quick_client
from voice_assistant.config import QUICK_PROVIDERS, load_config

ROOT = Path(__file__).resolve().parents[1]
PROFILE = Path.home() / ".cache/tars-browser/profile"
PORT = 9333
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TEXT_CHARS = 3000  # of the page's own text, after the list of things to click
ELEMENTS = 120  # at most, of the things to click or type into, in page order

# Marks every visible thing that can be clicked or typed into with data-tars, and returns them with the page's text.
VIEW_JS = r"""
() => {
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=tab], [role=menuitem],'
    + ' [role=checkbox], [role=option], [onclick], summary';
  const shown = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  // An open dialog is what a person is looking at, and all they can click: it's the view, not the page under it
  // (sites draw them outside <main>, at the end of the page).
  const dialogs = [...document.querySelectorAll('[aria-modal=true], dialog[open], [role=dialog]')].filter(shown);
  const dialog = dialogs[dialogs.length - 1] || null;
  const scope = dialog || document;
  document.querySelectorAll('[data-tars]').forEach((e) => e.removeAttribute('data-tars'));  // last view's numbers
  const seen = [];
  let n = 0;
  for (const el of scope.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < -200 || r.top > innerHeight * 3) continue;  // near the screen: what a person could reach soon
    // On screen, it must be what a click there would hit, not something under a pop-up or a backdrop.
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x >= 0 && y >= 0 && x < innerWidth && y < innerHeight) {
      const hit = document.elementFromPoint(x, y);
      if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) continue;
    }
    const secret = el.tagName === 'INPUT' && el.type === 'password';  // never its value: a model reads this
    const text = secret ? `(password field, ${el.value ? 'filled' : 'empty'})`
      : (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title')
         || el.getAttribute('placeholder') || el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const tag = el.tagName.toLowerCase();
    const kind = tag === 'a' ? 'link' : tag === 'input' ? `input:${el.type}` : el.getAttribute('role') || tag;
    if (!text && !tag.match(/input|select|textarea/)) continue;
    n += 1;
    el.setAttribute('data-tars', String(n));
    seen.push(`[${n}] ${kind} ${JSON.stringify(text)}`);
  }
  const main = dialog || document.querySelector('main, #a-page, [role=main]') || document.body;
  return {title: document.title, url: location.href, elements: seen, dialog: !!dialog,
          text: main.innerText.replace(/\n{3,}/g, '\n\n')};
}
"""

RULES = """You work a web browser for the person, toward their goal: {goal}

Each turn you get the page: its title and address, the things on it you can click or type into, numbered, and its \
text. Answer with the act tool: a list of steps, run in order, then you see the page again. Batch steps that don't \
need a new look (fill two fields and press the button). Prefer going straight to an address you know when it's \
quicker than clicking there.

Steps: {{"click": N}}, {{"type": N, "text": "...", "enter": true|false}}, {{"goto": "https://..."}}, \
{{"scroll": "down"|"up"}}, {{"back": true}}, {{"wait": seconds}}.

When the goal is reached, call done with the answer for them: short, plain, what they asked for. If the site asks \
you to sign in, call sign_in: TARS signs in with the login it keeps for that site (you never see it), asking the \
person for one first if it has none, and for any code the site sends (it can take minutes: just wait for it). Never \
type a password or a code yourself. If sign_in can't, or there's a CAPTCHA, call done saying so. Never buy, send, \
cancel or change anything, unless the goal is to order something: then you may fill the basket and go to the \
checkout, and there call request_order. Never press the button that places the order or pays: only TARS does, after \
the person approves on their phone."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Steps to take in order, then look at the page again.",
            "parameters": {
                "type": "object",
                "properties": {"steps": {"type": "array", "items": {"type": "object"}}},
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sign_in",
            "description": "Sign in to the site this page is on: with TARS's stored login, or, on a site that signs "
            "in by email, with one of the person's own addresses (their mailbox gets the code or link); says how it "
            "went.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "which of the person's addresses, for a site that signs in by email "
                        "(default: their first)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_order",
            "description": "At the checkout page: show the person the order and its total on their phone, and place "
            "it if they approve; says how it went.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Finish, with the answer for the person.",
            "parameters": {
                "type": "object",
                "properties": {"success": {"type": "boolean"}, "answer": {"type": "string"}},
                "required": ["success", "answer"],
            },
        },
    },
]


@contextmanager
def own_chrome(headless: bool):
    """A fresh TARS Chrome for as long as it's needed, then shut: real Chrome, its own profile, a debugging port, no
    automation flags. One already running is closed first, so nothing carries over but what the profile keeps on disk
    (a profile can only be open in one Chrome at a time anyway)."""
    close_chrome()
    PROFILE.mkdir(parents=True, exist_ok=True)
    flags = [f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}", "--no-first-run",
             "--no-default-browser-check"]  # fmt: skip
    if headless:
        flags += ["--headless=new", "--window-size=1280,1000"]
    process = subprocess.Popen([CHROME, *flags, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(75):
            if _answers():
                break
            time.sleep(0.2)
        else:
            raise SystemExit("TARS's Chrome didn't start")
        yield process
    finally:
        close_chrome()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()


def close_chrome() -> None:
    """Closes TARS's Chrome if it's running, the way quitting it would, so the profile is saved. Straight through its
    debugging socket: Playwright won't attach to a Chrome with no windows left (on a Mac, closing the last window
    leaves Chrome running)."""
    from websockets.sync.client import connect

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1) as answer:
            socket_url = json.load(answer)["webSocketDebuggerUrl"]
    except OSError:
        return
    with connect(socket_url, max_size=None) as socket:
        socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
    for _ in range(50):
        if not _answers():
            return
        time.sleep(0.2)


def _answers() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
        return True
    except OSError:
        return False


def view(page: Page) -> str:
    seen = page.evaluate(VIEW_JS)
    text = seen["text"].strip()
    if len(text) > TEXT_CHARS:
        text = text[:TEXT_CHARS] + " …"
    return "\n".join(
        [f"Title: {seen['title']}", f"Address: {seen['url']}"]
        + (
            ["A dialog is open over the page: this is what's in it (close it to get back to the page)."]
            if seen["dialog"]
            else []
        )
        + ["", "Things to click or type into:"]
        + seen["elements"][:ELEMENTS]
        + ([f"(and {len(seen['elements']) - ELEMENTS} more further down)"] if len(seen["elements"]) > ELEMENTS else [])
        + ["", "Text:", text]
    )


def why(e: Exception) -> str:
    """What went wrong with a step, briefly but with the reason: Playwright's first line is only "Timeout exceeded",
    and the lines after it say what's in the way."""
    lines = [line.strip() for line in str(e).splitlines() if line.strip()]
    reasons = [line for line in lines[1:] if any(w in line for w in ("intercepts", "not visible", "not enabled",
               "not stable", "outside of the viewport", "detached", "waiting for"))]  # fmt: skip
    return "; ".join([lines[0][:120], *[r[:160] for r in reasons[-2:]]]) if lines else type(e).__name__


def settle(page: Page) -> None:
    """Until the page has loaded what it's going to, briefly: a page that keeps polling isn't waited out."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
        page.wait_for_load_state("networkidle", timeout=2_500)
    except PageTimeout:
        return  # a page still busy is looked at as it is


def act(page: Page, steps: list[dict], failed: set[str] | None = None) -> list[str]:
    """Each step's outcome, stopping at the first that fails. `failed`: steps that already failed on this page; one
    asked for again isn't tried again (that's how an agent goes round in circles)."""
    said, failed = [], failed if failed is not None else set()
    for step in steps:
        key = json.dumps(step, sort_keys=True)
        if key in failed:
            said.append(
                f"not tried: {key} already failed here; do something else (look at the page, close a dialog, "
                "go straight to an address)"
            )
            break
        try:
            if "goto" in step:
                page.goto(str(step["goto"]), wait_until="domcontentloaded", timeout=20_000)
            elif "click" in step:
                target = page.locator(f'[data-tars="{int(step["click"])}"]').first
                label = target.inner_text(timeout=2_000) or target.get_attribute("aria-label") or ""
                if places_order(label):  # by code, after the person approves: never a model's click
                    said.append(f"refused: {label.strip()!r} places the order; call request_order instead")
                    break
                target.click(timeout=5_000)
            elif "type" in step:
                field = page.locator(f'[data-tars="{int(step["type"])}"]').first
                field.fill(str(step.get("text", "")), timeout=5_000)
                if step.get("enter"):
                    field.press("Enter")
            elif "scroll" in step:
                page.mouse.wheel(0, 900 if step["scroll"] != "up" else -900)
            elif "back" in step:
                page.go_back(wait_until="domcontentloaded", timeout=15_000)
            elif "wait" in step:
                page.wait_for_timeout(min(float(step["wait"]), 5) * 1000)
            else:
                said.append(f"unknown step {step}")
                break
            settle(page)
            said.append(f"ok: {json.dumps(step)}")
        except Exception as e:  # noqa: BLE001 - the model hears what went wrong and tries something else
            failed.add(key)
            said.append(f"failed: {key}: {why(e)}")
            break
    return said


PAGE = "Title:"  # how a page view starts: only the newest is sent in full


class ChatBrain:
    """Qwen on Groq, through chat completions."""

    def __init__(self, client, model: str, goal: str, first: str):
        self.client, self.model = client, model
        self.messages: list[dict] = [
            {"role": "system", "content": RULES.format(goal=goal)},
            {"role": "user", "content": first},
        ]
        self.tokens_in = self.tokens_out = 0

    def ask(self) -> list[tuple[str, str, dict]]:
        reply = self.client.chat.completions.create(
            model=self.model, messages=self.messages, tools=TOOLS, tool_choice="required", max_completion_tokens=1500
        )
        if reply.usage:
            self.tokens_in += reply.usage.prompt_tokens
            self.tokens_out += reply.usage.completion_tokens
        calls = reply.choices[0].message.tool_calls or []
        self.messages.append({"role": "assistant", "tool_calls": [c.model_dump() for c in calls], "content": ""})
        return [(c.id, c.function.name, json.loads(c.function.arguments or "{}")) for c in calls]

    def answer(self, call_id: str, output: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": output})

    def page(self, looked: str) -> None:
        for m in self.messages:
            if m["role"] == "user" and m["content"].startswith(PAGE):
                m["content"] = "(an earlier page)"
        self.messages.append({"role": "user", "content": looked})


class ResponsesBrain:
    """Luna on OpenAI, through the Responses API (its only way to call tools while reasoning), as the TV agent does."""

    def __init__(self, client, model: str, goal: str, first: str):
        self.client, self.model, self.instructions = client, model, RULES.format(goal=goal)
        self.items: list[dict] = [{"role": "user", "content": first}]
        self.tools = [{"type": "function", **t["function"]} for t in TOOLS]
        self.tokens_in = self.tokens_out = 0

    def ask(self) -> list[tuple[str, str, dict]]:
        reply = self.client.responses.create(
            model=self.model,
            instructions=self.instructions,
            input=self.items,
            tools=self.tools,
            tool_choice="required",
            store=False,
            reasoning={"effort": "low"},
        )
        if reply.usage:
            self.tokens_in += reply.usage.input_tokens
            self.tokens_out += reply.usage.output_tokens
        calls = [i for i in reply.output if getattr(i, "type", "") == "function_call"]
        self.items += [
            {"type": "function_call", "call_id": c.call_id, "name": c.name, "arguments": c.arguments} for c in calls
        ]
        return [(c.call_id, c.name, json.loads(c.arguments or "{}")) for c in calls]

    def answer(self, call_id: str, output: str) -> None:
        self.items.append({"type": "function_call_output", "call_id": call_id, "output": output})

    def page(self, looked: str) -> None:
        for i in self.items:
            if i.get("role") == "user" and str(i.get("content", "")).startswith(PAGE):
                i["content"] = "(an earlier page)"
        self.items.append({"role": "user", "content": looked})


def brain(which: str, goal: str, first: str):
    """qwen (Groq, the quick brain) or luna (OpenAI)."""
    cfg = load_config(ROOT / "config.toml")
    env = ROOT / ".env"
    if which == "luna":
        return ResponsesBrain(make_openai_client(env), cfg.llm.model, goal, first)
    groq = next(p for p in QUICK_PROVIDERS if p.name == "Groq")
    return ChatBrain(make_quick_client(env, groq), cfg.llm.groq_model, goal, first)


def run(goal: str, which: str, steps: int, headless: bool, whose: str = "", really_order: bool = False) -> None:
    with own_chrome(headless=headless), sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        mind = brain(which, goal, view(page))
        person = Person(tailscale_address(), whose)
        signed_in, failed, last_url, stuck = False, set(), page.url, 0
        started = time.perf_counter()
        for n in range(1, steps + 1):
            t = time.perf_counter()
            calls = mind.ask()
            thought = time.perf_counter() - t
            if not calls:
                print(f"step {n}: no tool call")
                break
            finished = None
            for call_id, name, args in calls:
                if name == "done":
                    finished = args
                    mind.answer(call_id, "ok")
                    continue
                t2 = time.perf_counter()
                if name == "sign_in" and signed_in:  # once a task: retrying sign-ins is how accounts get locked
                    outcome = ["refused: sign_in already ran for this task. Call done and say what stopped it."]
                elif name == "request_order":  # by code, after the person approves
                    outcome = [request_order(page, person, goal, really_order)]
                elif name == "sign_in":  # by code: the login never reaches the model
                    signed_in = True
                    outcome = [sign_in(page, person, goal, str(args.get("email") or ""))]
                else:
                    if page.url != last_url:  # a new page: what failed on the last one may work here
                        failed, last_url = set(), page.url
                    outcome = act(page, list(args.get("steps", [])), failed)
                looked = view(page)
                worked = time.perf_counter() - t2
                print(f"step {n}: model {thought:.1f}s, browser {worked:.1f}s, view {len(looked)} chars")
                for line in outcome:
                    print(f"    {line}")
                stuck = stuck + 1 if outcome and outcome[-1].startswith("not tried") else 0
                if stuck >= 2:  # asked twice for what already failed: it's going round in circles, so stop
                    print(
                        f"\nstuck after {n} steps, {time.perf_counter() - started:.1f}s: it kept asking for a step "
                        "that already failed"
                    )
                    return
                mind.answer(call_id, "\n".join(outcome))
                mind.page(looked)
            if finished is not None:
                took = time.perf_counter() - started
                used = f"{mind.tokens_in} tokens in / {mind.tokens_out} out"
                print(f"\ndone in {n} steps, {took:.1f}s, {used} ({mind.model})")
                print(f"success: {finished.get('success')}\n{finished.get('answer')}")
                return
        print(f"\nstopped after {steps} steps, {time.perf_counter() - started:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("signin")
    s.add_argument("url", nargs="?", default="https://www.amazon.com/gp/css/order-history")
    v = sub.add_parser("view")
    v.add_argument("url", nargs="?")
    r = sub.add_parser("run")
    r.add_argument("goal")
    r.add_argument("--model", choices=("qwen", "luna"), default="qwen")
    r.add_argument("--steps", type=int, default=12)
    r.add_argument("--headless", action="store_true", help="no window (Amazon takes it for a bot: see below)")
    r.add_argument("--person", default="", help="whose task: a name in [telegram] people (default: the first)")
    r.add_argument("--really-order", action="store_true", help="place an approved order (default: stop at the button)")
    args = parser.parse_args()
    if args.command == "run":
        run(args.goal, args.model, args.steps, args.headless, args.person, args.really_order)
        return
    signing_in = args.command == "signin"
    with own_chrome(headless=False) as process, sync_playwright() as p:
        context = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}").contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        if signing_in:
            page.goto(args.url)
            print("Sign in by hand in the Chrome window that opened (tick 'Keep me signed in'), then quit that Chrome")
            print("(Cmd-Q in its window): what you signed into is kept in its profile, and this ends.")
            process.wait()
            return
        if args.url:
            page.goto(args.url)
            settle(page)
        print(view(page))


if __name__ == "__main__":
    main()
