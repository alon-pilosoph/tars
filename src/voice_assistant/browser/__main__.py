"""TARS's browser from a terminal.

    uv run --extra browser python -m voice_assistant.browser signin [URL] [--vnc]   # sign in by hand (--vnc: on the Pi)
    uv run --extra browser python -m voice_assistant.browser view URL [--shot FILE]   # what the agent sees there
    uv run --extra browser python -m voice_assistant.browser run "GOAL" [--turns N] [--trace FILE]

`run` asks for approval in the terminal (y/n) before anything is committed, and presses the button only with
--really-commit; without it, an approved commit is a dry run."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from .agent import Settings
from .chrome import PROFILE, own_chrome
from .run import run_task
from .session import Session


class Terminal:
    """The person, at this terminal."""

    def approve(self, draft: str, picture: bytes | None = None) -> bool:
        print(f"\n{draft}\n")
        return input("Approve? [y/N] ").strip().lower() in ("y", "yes")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m voice_assistant.browser", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("signin", help="a TARS Chrome to sign in to sites by hand")
    s.add_argument("url", nargs="?", default="about:blank")
    s.add_argument("--vnc", action="store_true", help="no screen here (the Pi): show it as a web page to sign in on")
    s.add_argument("--vnc-host", default="127.0.0.1", help="where that page listens (default: this machine only)")
    v = sub.add_parser("view", help="print what the agent is shown of a page")
    v.add_argument("url")
    v.add_argument("--shot", type=Path, help="also save the screenshot it would get, refs drawn on")
    v.add_argument("--wait", type=float, default=2.0, help="seconds to let the page load first")
    r = sub.add_parser("run", help="the agent, on one goal")
    r.add_argument("goal")
    r.add_argument("--turns", type=int, default=30)
    r.add_argument("--trace", type=Path)
    r.add_argument("--really-commit", action="store_true", help="press an approved commit (default: a dry run)")
    args = parser.parse_args()
    if args.command == "run":
        settings = Settings(turns=args.turns, really_commit=args.really_commit)
        result = run_task(args.goal, Terminal(), trace_to=args.trace, settings=settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    with own_chrome(PROFILE) as address, sync_playwright() as p:
        context = p.chromium.connect_over_cdp(address).contexts[0]
        session = Session(context)
        if args.command == "signin":
            session.page.goto(args.url)
            if not args.vnc:
                print("Sign in by hand in that Chrome window (tick 'Keep me signed in'), then press Enter here.")
                input()
                return
            from .vnc import vnc_view

            with vnc_view(args.vnc_host) as (url, password):
                print(f"Open {url} (password {password}), sign in there (tick 'Keep me signed in'), then press "
                      "Enter here. The page and its password stop working when this ends.")  # fmt: skip
                input()
            return
        session.page.goto(args.url, wait_until="domcontentloaded")
        session.page.wait_for_timeout(args.wait * 1000)
        print(session.look().text)
        if args.shot:
            args.shot.write_bytes(session.screenshot())


if __name__ == "__main__":
    main()
