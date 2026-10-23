"""A prototype of "take over": TARS's browser shown live on a web page, worked by hand from a phone (over Tailscale)
or any browser, to sign in or get past a code or a CAPTCHA. Press Done and TARS has its browser back.

    uv run --all-extras python tools/browser_takeover.py [URL] [--host 127.0.0.1] [--port 8090]

It starts TARS's Chrome (tools/browser_proto.py) and serves one page, its link carrying a one-time token. While
someone has it open, Chrome is made the shape of their screen (the way DevTools' device mode does it: their width,
their pixel ratio, touch), so a site shows its phone layout at full size and a finger scrolls it. The screen comes from
Chrome's own screencast; touches and clicks go back in as input events. Typing goes through a real text box at the
bottom, which takes the shape of the field tapped on the page (a password field becomes a password box, so the
phone's password manager offers to fill it), and whatever is in it is put into that field. Done gives Chrome its
own shape back. Chrome's debugging port never leaves this machine; only this page does, and only with the token."""

import argparse
import asyncio
import json
import secrets
from contextlib import suppress

import uvicorn
from browser_proto import PORT, own_chrome
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

TOKEN = secrets.token_urlsafe(16)
# CDP's key events for the keys that aren't text: what they're called, and Windows' code for each.
KEYS = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27}
FOCUSED_JS = """(() => {
  let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return null;
  const tag = e.tagName, kind = tag === 'INPUT' ? (e.type || 'text') : 'text';
  const notText = ['button', 'submit', 'checkbox', 'radio', 'file', 'image', 'reset', 'range', 'color', 'hidden'];
  const editable = (tag === 'INPUT' && !notText.includes(kind)) || tag === 'TEXTAREA' || e.isContentEditable;
  if (!editable) return null;
  return {type: kind, autocomplete: e.autocomplete || '',
          label: e.getAttribute('aria-label') || e.placeholder || e.name || e.id || '',
          value: kind === 'password' ? '' : String(e.value ?? e.innerText ?? '').slice(0, 500)};
})()"""
SELECT_FOCUSED_JS = """(() => {
  let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return false;
  if (e.select) e.select(); else if (e.isContentEditable) document.execCommand('selectAll');
  return true;
})()"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>TARS's browser</title>
<style>
  html, body { margin: 0; height: 100%; background: #14161a; color: #e8e6e1; font: 16px system-ui, sans-serif;
               overscroll-behavior: none; }
  body { display: flex; flex-direction: column; }
  .bar { display: flex; gap: 6px; padding: 6px; background: #14161a; flex: none; }
  input { flex: 1; min-width: 0; padding: 8px; border-radius: 8px; border: 1px solid #444; background: #22252b;
          color: inherit; font: inherit; }
  button { padding: 8px 12px; border-radius: 8px; border: 1px solid #444; background: #2b2f36; color: inherit;
           font: inherit; }
  #done { background: #3a5f9e; border-color: #3a5f9e; }
  #view { flex: 1; overflow: hidden; position: relative; }
  #screen { display: block; width: 100%; touch-action: none; user-select: none; -webkit-user-select: none;
            -webkit-touch-callout: none; }
  #note { padding: 2em; }
</style></head>
<body>
<div class="bar">
  <button id="back" title="Back">←</button>
  <input id="url" placeholder="Address" autocapitalize="off" autocomplete="off" spellcheck="false" inputmode="url">
  <button id="done">Done</button>
</div>
<div id="view"><img id="screen" alt="TARS's browser"></div>
<div class="bar">
  <input id="type" placeholder="Tap a field on the page, then type here" autocapitalize="off" autocorrect="off"
         spellcheck="false" autocomplete="off">
  <button id="enter">⏎</button>
</div>
<script>
const $ = (id) => document.getElementById(id);
const screen = $("screen"), view = $("view"), url = $("url"), typing = $("type");
let ws = null, ended = false;
const send = (o) => ws && ws.readyState === 1 && ws.send(JSON.stringify(o));
let meta = { deviceWidth: 1, deviceHeight: 1 };
const touch = matchMedia("(pointer: coarse)").matches;
const box = view.getBoundingClientRect();  // measured once: the keyboard coming up mustn't reshape Chrome
// Connects, and connects again if the connection drops (a phone going to sleep, a network change), until Done.
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${location.search}`);
  ws.onopen = () => {  // Chrome becomes this shape: the page shows 1:1, nothing shrunk
    view.style.opacity = 1;
    send({ hello: { width: Math.round(box.width), height: Math.round(box.height), dpr: devicePixelRatio || 1,
                    touch } });
  };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.frame) { screen.src = "data:image/jpeg;base64," + m.frame; meta = m.meta; }
    if (m.url !== undefined && document.activeElement !== url) url.value = m.url;
    if ("focus" in m) shape(m.focus);
    if (m.ended) {
      ended = true;
      document.body.innerHTML = '<p id="note">Handed back to TARS. You can close this page.</p>';
    }
  };
  ws.onclose = () => { if (!ended) { view.style.opacity = 0.5; setTimeout(connect, 1000); } };
}
connect();

// The typing box takes the shape of the field tapped on the page, so the keyboard and password manager fit it.
function shape(f) {
  if (!f) { typing.type = "text"; typing.autocomplete = "off"; typing.placeholder = "Tap a field on the page"; return; }
  const password = f.type === "password";
  typing.type = password ? "password" : ["email", "tel", "url", "number", "search"].includes(f.type) ? f.type : "text";
  typing.autocomplete = password ? (f.autocomplete === "new-password" ? "new-password" : "current-password")
    : f.type === "email" || /user|email/.test(f.autocomplete + f.label) ? "username" : "off";
  typing.placeholder = f.label || (password ? "Password" : "Type here");
  typing.value = f.value || "";
}
typing.addEventListener("input", () => send({ set: typing.value }));
typing.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); send({ key: e.key }); }
});
$("enter").addEventListener("pointerdown", (e) => { e.preventDefault(); send({ key: "Enter" }); });

// A finger goes in as touches (so a swipe scrolls), a mouse as a mouse. Neither takes the focus off the typing box,
// so the keyboard stays up while fields are tapped on the page.
const at = (x, y) => {
  const r = screen.getBoundingClientRect();
  return { x: (x - r.left) * meta.deviceWidth / r.width, y: (y - r.top) * meta.deviceHeight / r.height };
};
const points = (e) => [...e.touches].map((t) => at(t.clientX, t.clientY));
// Moves go at most once a frame, and a move still waiting goes before the finger lifts: Chrome refuses a touch that
// moves after it ended.
let moved = null, pending = 0;
const flush = () => { pending = 0; if (moved) send({ touch: "touchMove", points: moved }); moved = null; };
screen.addEventListener("touchstart", (e) => { e.preventDefault(); send({ touch: "touchStart", points: points(e) }); },
  { passive: false });
screen.addEventListener("touchmove", (e) => {
  e.preventDefault();
  moved = points(e);
  if (!pending) pending = requestAnimationFrame(flush);
}, { passive: false });
const lifted = (e) => {
  e.preventDefault();
  if (pending) { cancelAnimationFrame(pending); flush(); }
  send(e.touches.length ? { touch: "touchMove", points: points(e) } : { touch: "touchEnd", points: [] });
};
screen.addEventListener("touchend", lifted, { passive: false });
screen.addEventListener("touchcancel", lifted, { passive: false });
screen.addEventListener("mousedown", (e) => {
  e.preventDefault();
  send({ mouse: "mousePressed", ...at(e.clientX, e.clientY) });
});
screen.addEventListener("mouseup", (e) => send({ mouse: "mouseReleased", ...at(e.clientX, e.clientY) }));
screen.addEventListener("mousemove", (e) => {
  if (e.buttons) send({ mouse: "mouseMoved", ...at(e.clientX, e.clientY) });
});
screen.addEventListener("wheel", (e) => {
  e.preventDefault();
  send({ wheel: true, ...at(e.clientX, e.clientY), dx: e.deltaX, dy: e.deltaY });
}, { passive: false });

$("back").onclick = () => send({ back: true });
url.addEventListener("keydown", (e) => { if (e.key === "Enter") { send({ goto: url.value }); url.blur(); } });
$("done").onclick = () => send({ done: true });
</script>
</body></html>"""


class Takeover:
    """TARS's browser, the shape of someone's screen, streamed to them and worked by them, until they press Done."""

    def __init__(self, context):
        self.context = context
        self.page = context.pages[-1] if context.pages else None
        self.done = asyncio.Event()
        self.shape: dict | None = None
        self.cdp = None
        self.ws: WebSocket | None = None
        self.desktop_agent = ""  # Chrome's own identity, put back on Done
        self.touching = False  # a finger is down: Chrome takes a touch's moves and end only after its start
        context.on("page", lambda page: asyncio.ensure_future(self.watch(page)))

    async def watch(self, page) -> None:
        """Stream `page` to whoever has the take-over page open, in their shape."""
        if self.cdp is not None:
            with suppress(Exception):
                await self.cdp.send("Page.stopScreencast")
        self.page, self.cdp = page, await self.context.new_cdp_session(page)
        self.touching = False
        cdp = self.cdp
        cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(self.frame(cdp, params)))
        width, height, dpr = 1280, 1000, 1
        if self.shape:
            width, height, dpr = self.shape["width"], self.shape["height"], self.shape["dpr"]
            touch = bool(self.shape.get("touch"))
            await cdp.send("Emulation.setDeviceMetricsOverride",
                           {"width": width, "height": height, "deviceScaleFactor": dpr, "mobile": touch})  # fmt: skip
            await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": touch, "maxTouchPoints": 5})
            if touch:  # sites choose their phone layout by what the browser says it is, as in DevTools' device mode
                await cdp.send("Emulation.setUserAgentOverride", await self.phone_agent())
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 75, "everyNthFrame": 1,
                       "maxWidth": int(width * dpr), "maxHeight": int(height * dpr)})  # fmt: skip

    async def phone_agent(self) -> dict:
        """Chrome on an Android phone, of this Chrome's own version."""
        if not self.desktop_agent:
            self.desktop_agent = await self.page.evaluate("navigator.userAgent")
        version = next(
            (p.split("/")[1] for p in self.desktop_agent.split() if p.startswith(("Chrome/", "HeadlessChrome/"))), ""
        )
        major = version.split(".")[0]
        brands = [{"brand": "Google Chrome", "version": major}, {"brand": "Chromium", "version": major},
                  {"brand": "Not(A:Brand", "version": "24"}]  # fmt: skip
        return {
            "userAgent": f"Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} "
            "Mobile Safari/537.36",
            "platform": "Linux armv8l",
            "userAgentMetadata": {
                "brands": brands,
                "platform": "Android",
                "platformVersion": "14.0.0",
                "architecture": "",
                "model": "",
                "mobile": True,
            },
        }

    async def frame(self, cdp, params: dict) -> None:
        with suppress(Exception):  # the page may have closed under it, or the phone gone
            await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            if cdp is self.cdp and self.ws is not None:
                await self.ws.send_text(json.dumps({"frame": params["data"], "meta": params["metadata"],
                                                    "url": self.page.url}))  # fmt: skip

    async def unshape(self) -> None:
        """Chrome its own shape again, for TARS."""
        if self.cdp is None:
            return
        with suppress(Exception):
            await self.cdp.send("Page.stopScreencast")
            await self.cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": False})
            await self.cdp.send("Emulation.clearDeviceMetricsOverride")
            if self.desktop_agent:
                await self.cdp.send("Emulation.setUserAgentOverride", {"userAgent": self.desktop_agent})

    async def report_focus(self) -> None:
        """Tells the page what field is focused now, so its typing box takes that shape."""
        await asyncio.sleep(0.25)  # the tap's focus change lands first
        with suppress(Exception):
            focused = await self.page.evaluate(FOCUSED_JS)
            await self.ws.send_text(json.dumps({"focus": focused}))

    async def serve(self, ws: WebSocket) -> None:
        self.ws = ws
        if self.page is None:
            self.page = await self.context.new_page()
        try:
            while not self.done.is_set():
                message = json.loads(await ws.receive_text())
                try:
                    await self.handle(message)
                except PlaywrightError as e:  # one input the page refused isn't worth the connection
                    print(f"(take over: {next(iter(message), '?')} refused: {str(e).splitlines()[0]})", flush=True)
        except WebSocketDisconnect:
            await self.unshape()
            return
        await self.unshape()
        with suppress(Exception):
            await ws.send_text(json.dumps({"ended": True}))

    async def handle(self, m: dict) -> None:
        if "hello" in m:
            self.shape = m["hello"]
            await self.watch(self.page)
            if self.shape.get("touch") and self.page.url.startswith("http"):
                with suppress(Exception):  # loaded again as a phone, for the phone layout
                    await self.page.reload(wait_until="commit", timeout=20_000)
        elif self.cdp is None:
            return
        elif "touch" in m:
            kind = m["touch"]
            if kind == "touchStart" and self.touching:  # the last one never ended: end it first
                await self.cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            elif kind != "touchStart" and not self.touching:
                return  # what's left of a touch that broke off (the page changed under it)
            self.touching = kind != "touchEnd"
            await self.cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": m["points"]})
            if kind == "touchEnd":
                asyncio.ensure_future(self.report_focus())
        elif "mouse" in m:
            await self.cdp.send("Input.dispatchMouseEvent", {"type": m["mouse"], "x": m["x"], "y": m["y"],
                                "button": "left", "clickCount": 0 if m["mouse"] == "mouseMoved" else 1})  # fmt: skip
            if m["mouse"] == "mouseReleased":
                asyncio.ensure_future(self.report_focus())
        elif "wheel" in m:
            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": m["x"], "y": m["y"],
                                "deltaX": m["dx"], "deltaY": m["dy"]})  # fmt: skip
        elif "set" in m:  # the typing box's whole text, into the focused field in place of what's there
            if await self.page.evaluate(SELECT_FOCUSED_JS):
                if m["set"]:
                    await self.cdp.send("Input.insertText", {"text": str(m["set"])})
                else:
                    await self.key("Backspace")
        elif "key" in m and m["key"] in KEYS:
            await self.key(m["key"])
            asyncio.ensure_future(self.report_focus())
        elif "goto" in m:
            address = str(m["goto"]).strip()
            if address and "://" not in address:
                address = "https://" + address
            with suppress(Exception):
                await self.page.goto(address, wait_until="commit", timeout=20_000)
        elif "back" in m:
            with suppress(Exception):
                await self.page.go_back(wait_until="commit", timeout=15_000)
        elif "done" in m:
            self.done.set()

    async def key(self, key: str) -> None:
        down = {"type": "keyDown", "key": key, "code": key, "windowsVirtualKeyCode": KEYS[key]}
        if key == "Enter":
            down["text"] = "\r"
        await self.cdp.send("Input.dispatchKeyEvent", down)
        await self.cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": key,
                                                       "windowsVirtualKeyCode": KEYS[key]})  # fmt: skip


async def serve(start_url: str | None, host: str, port: int) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        takeover = Takeover(browser.contexts[0])
        if start_url:
            takeover.page = takeover.page or await browser.contexts[0].new_page()
            await takeover.page.goto(start_url, wait_until="commit")
        app = FastAPI()

        @app.get("/", response_model=None)
        async def page(token: str = "") -> HTMLResponse | PlainTextResponse:
            if not secrets.compare_digest(token, TOKEN):
                return PlainTextResponse("Not without the link TARS sent.", status_code=403)
            return HTMLResponse(PAGE)

        @app.websocket("/ws")
        async def socket(ws: WebSocket, token: str = "") -> None:
            if not secrets.compare_digest(token, TOKEN):
                await ws.close(code=1008)
                return
            await ws.accept()
            await takeover.serve(ws)

        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
        print(f"Take over TARS's browser at http://{host}:{port}/?token={TOKEN}", flush=True)
        serving = asyncio.ensure_future(server.serve())
        await takeover.done.wait()
        await asyncio.sleep(0.3)  # the page hears it's handed back
        server.should_exit = True
        await serving


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="where to open it (default: as it is)")
    parser.add_argument(
        "--host", default="127.0.0.1", help="this machine's Tailscale address, to reach it from a phone"
    )
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    with own_chrome(headless=False):
        asyncio.run(serve(args.url, args.host, args.port))
    print("Handed back: TARS's Chrome is closed, and what was signed into is kept in its profile.")


if __name__ == "__main__":
    main()
