"""A prototype of "take over": TARS's browser shown live on a web page, worked by hand from any browser (a phone
over Tailscale, say), to sign in or get past a code or a CAPTCHA. Press Done and TARS has its browser back.

    uv run --all-extras python tools/browser_takeover.py [URL] [--host 127.0.0.1] [--port 8090]

It starts TARS's Chrome (tools/browser_proto.py) and serves one page, its link carrying a one-time token: the screen
comes from Chrome's own screencast, and taps, scrolling and typing go back in as input events. Chrome's debugging
port never leaves this machine; only this page does, and only with the token."""

import argparse
import asyncio
import json
import secrets
from contextlib import suppress

import uvicorn
from browser_proto import PORT, own_chrome
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
from playwright.async_api import async_playwright

TOKEN = secrets.token_urlsafe(16)
# CDP's key events for the keys that aren't text: what they're called, and Windows' code for each.
KEYS = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46,
        "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40}  # fmt: skip

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TARS's browser</title>
<style>
  body { margin: 0; background: #14161a; color: #e8e6e1; font: 15px system-ui, sans-serif; }
  header { display: flex; gap: 6px; padding: 8px; position: sticky; top: 0; background: #14161a; }
  input#url { flex: 1; min-width: 0; padding: 6px 8px; border-radius: 6px; border: 1px solid #444;
              background: #22252b; color: inherit; font: inherit; }
  button { padding: 6px 10px; border-radius: 6px; border: 1px solid #444; background: #2b2f36; color: inherit;
           font: inherit; }
  button#done { background: #3a5f9e; border-color: #3a5f9e; }
  #screen { display: block; width: 100%; touch-action: none; user-select: none; -webkit-user-select: none; }
  #typer { position: fixed; left: -1000px; top: 0; opacity: 0; }
  #note { padding: 2em; }
</style></head>
<body>
<header>
  <button id="back" title="Back">←</button>
  <input id="url" placeholder="Address" autocapitalize="off" autocomplete="off" spellcheck="false">
  <button id="keys" title="Keyboard">⌨</button>
  <button id="done">Done</button>
</header>
<img id="screen" alt="TARS's browser">
<input id="typer" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false">
<script>
const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${location.search}`);
const screen = document.getElementById("screen"), url = document.getElementById("url");
const typer = document.getElementById("typer");
let meta = { deviceWidth: 1, deviceHeight: 1 };
const send = (o) => ws.readyState === 1 && ws.send(JSON.stringify(o));
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.frame) { screen.src = "data:image/jpeg;base64," + m.frame; meta = m.meta; }
  if (m.url !== undefined && document.activeElement !== url) url.value = m.url;
  if (m.ended) document.body.innerHTML = '<p id="note">Handed back to TARS. You can close this page.</p>';
};
ws.onclose = () => { if (!document.getElementById("note")) document.body.insertAdjacentHTML("beforeend",
  '<p id="note">The connection to TARS ended.</p>'); };
const at = (e) => {
  const r = screen.getBoundingClientRect();
  return { x: (e.clientX - r.left) / r.width * meta.deviceWidth,
           y: (e.clientY - r.top) / r.height * meta.deviceHeight };
};
screen.addEventListener("pointerdown", (e) => { e.preventDefault(); send({ mouse: "mousePressed", ...at(e) }); });
screen.addEventListener("pointerup", (e) => { send({ mouse: "mouseReleased", ...at(e) });
  typer.focus({ preventScroll: true }); });  // what's typed next goes into whatever was tapped
screen.addEventListener("pointermove", (e) => { if (e.buttons) send({ mouse: "mouseMoved", ...at(e) }); });
screen.addEventListener("wheel", (e) => { e.preventDefault();
  send({ wheel: true, ...at(e), dx: e.deltaX, dy: e.deltaY }); }, { passive: false });
typer.addEventListener("input", () => { if (typer.value) { send({ text: typer.value }); typer.value = ""; } });
typer.addEventListener("keydown", (e) => {
  if (["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"]
      .includes(e.key)) { e.preventDefault(); send({ key: e.key }); }
});
document.getElementById("keys").onclick = () => typer.focus();
document.getElementById("back").onclick = () => send({ back: true });
url.addEventListener("keydown", (e) => { if (e.key === "Enter") { send({ goto: url.value }); url.blur(); } });
document.getElementById("done").onclick = () => send({ done: true });
</script>
</body></html>"""


class Takeover:
    """TARS's browser, streamed to whoever has the page open and worked by them, until they press Done."""

    def __init__(self, context):
        self.context = context
        self.page = context.pages[-1] if context.pages else None
        self.done = asyncio.Event()

    async def serve(self, ws: WebSocket) -> None:
        if self.page is None:
            self.page = await self.context.new_page()
        cdp = await self.context.new_cdp_session(self.page)
        watching = cdp

        async def frame(params: dict) -> None:
            with suppress(Exception):  # the page may have closed under it
                await watching.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                await ws.send_text(json.dumps({"frame": params["data"], "meta": params["metadata"],
                                               "url": self.page.url}))  # fmt: skip

        async def start(on) -> None:
            on.on("Page.screencastFrame", lambda params: asyncio.ensure_future(frame(params)))
            await on.send("Page.startScreencast", {"format": "jpeg", "quality": 70, "maxWidth": 1600,
                                                   "maxHeight": 1600, "everyNthFrame": 1})  # fmt: skip

        async def follow(page) -> None:
            """A link that opens a new tab: show that tab instead."""
            nonlocal watching, cdp
            with suppress(Exception):
                await watching.send("Page.stopScreencast")
            self.page = page
            cdp = watching = await self.context.new_cdp_session(page)
            await start(watching)

        self.context.on("page", lambda page: asyncio.ensure_future(follow(page)))
        await start(cdp)
        try:
            while not self.done.is_set():
                message = json.loads(await ws.receive_text())
                await self.handle(message, cdp)
        except WebSocketDisconnect:
            return
        with suppress(Exception):
            await ws.send_text(json.dumps({"ended": True}))

    async def handle(self, m: dict, cdp) -> None:
        if "mouse" in m:
            await cdp.send("Input.dispatchMouseEvent", {"type": m["mouse"], "x": m["x"], "y": m["y"],
                           "button": "left", "clickCount": 0 if m["mouse"] == "mouseMoved" else 1})  # fmt: skip
        elif "wheel" in m:
            await cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": m["x"], "y": m["y"],
                           "deltaX": m["dx"], "deltaY": m["dy"]})  # fmt: skip
        elif "text" in m:
            await cdp.send("Input.insertText", {"text": str(m["text"])})
        elif "key" in m and m["key"] in KEYS:
            key, code = m["key"], KEYS[m["key"]]
            down = {"type": "keyDown", "key": key, "code": key, "windowsVirtualKeyCode": code}
            if key == "Enter":
                down["text"] = "\r"
            await cdp.send("Input.dispatchKeyEvent", down)
            await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": key,
                                                      "windowsVirtualKeyCode": code})  # fmt: skip
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
