"""TARS's browser from a terminal.

    uv run --extra browser python -m voice_assistant.browser run "GOAL" [--turns N] [--trace FILE] [--person NAME]
    uv run --extra browser python -m voice_assistant.browser vault add|mail|list|remove ...   # TARS's logins
    uv run --extra browser python -m voice_assistant.browser view URL [--shot FILE]   # what the agent sees there
    uv run --extra browser python -m voice_assistant.browser signin [URL]   # sign in by hand, on a desktop

`run` asks in the terminal for approval (y/n) before anything is committed, and presses the button only with
--really-commit; without it, an approved commit is a dry run. A login TARS lacks, or a code a site sends, is asked
for there too. `signin` is for a site TARS can't sign in to with a login (one that signs in with Google, say)."""

import argparse
import getpass
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from .agent import Settings
from .chrome import PROFILE, own_chrome
from .login import MAIL_WAIT_S, Answer
from .login_page import Field
from .run import ROOT, run_task
from .session import Session
from .vault import GMAIL, Vault, household_person, origin


class Terminal:
    """The person, at this terminal: asked to approve a commit, and for a login or a code TARS lacks."""

    def __init__(self, name: str):
        self.name = name

    def approve(self, draft: str, picture: bytes | None = None) -> bool:
        print(f"\n{draft}\n")
        return input("Approve? [y/N] ").strip().lower() in ("y", "yes")

    def login(self, site: str, goal: str, fields: list[Field]) -> dict[str, str] | None:
        print(f"\nTARS needs {self.name}'s login for {site}. It's kept encrypted, typed only into {site}, and never "
              "shown to the model. (Enter on its own: none.)")  # fmt: skip
        values = {}
        for f in fields:
            value = getpass.getpass(f"{f.label} (not shown): ") if f.secret else input(f"{f.label}: ").strip()
            if f.required and not value:
                return None
            values[f.key] = value
        return values

    def code(self, site: str, answer: Answer) -> str | None:
        if answer.mail and answer.wait(MAIL_WAIT_S):  # input() can't be raced: the email gets its chance first
            print(f"(the code {site} sent came by email)")
            return answer.code
        return input(f"\n{site} has sent {self.name} a sign-in code. The code: ").strip() or None


def vault_command(args: argparse.Namespace) -> None:
    vault = Vault()
    if args.action == "list":
        for person, logins in vault.everyone().items():
            for login in logins:
                print(f"{person}: {login.name}: {login.username} on {', '.join(login.origins)}")
        if not any(vault.everyone().values()):
            print("(no logins)")
        return
    if args.action == "remove":
        print("removed" if vault.remove(args.person, args.site) else f"{args.person} has no login {args.site}")
        return
    if args.action == "mail":
        print("A Gmail app password, not the Google password: myaccount.google.com/apppasswords (it can be revoked "
              "there any time).")  # fmt: skip
        password = getpass.getpass(f"{args.site}'s app password (not shown): ")
        try:
            login = vault.store_mailbox(args.person, args.site, password, args.server)
        except ValueError as e:
            raise SystemExit(f"nothing kept: {e}") from None
        print(f"Kept {args.person}'s mailbox {login.username}: TARS reads sign-in emails from it.")
        return
    sites = [args.site, *args.also]
    username = input(f"{args.person}'s email or phone on {origin(args.site)}: ").strip()
    password = getpass.getpass("Password (not shown; Enter if it signs in by a code alone): ")
    try:  # anything else the site asks for is asked when it first does
        login = vault.store(args.person, sites, {"username": username, "password": password})
    except ValueError as e:
        raise SystemExit(f"nothing kept: {e}") from None
    print(f"Kept {args.person}'s login {login.name}, for {', '.join(login.origins)}.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m voice_assistant.browser", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = parser.add_subparsers(dest="command", required=True)
    person = household_person(ROOT / "config.toml")
    s = sub.add_parser("signin", help="a TARS Chrome to sign in to a site by hand (on a desktop)")
    s.add_argument("url", nargs="?", default="about:blank")
    k = sub.add_parser("vault", help="TARS's logins: add a site's, mail (a mailbox to read codes from), list, remove")
    k.add_argument("action", choices=("add", "mail", "list", "remove"))
    k.add_argument("site", nargs="?", help="add: the site's address (https://www.ksp.co.il); mail: the email "
                   "address; remove: the login's name, as list shows it")  # fmt: skip
    k.add_argument("--server", default=GMAIL, help=f"mail: its IMAP server (default {GMAIL})")
    k.add_argument("--also", action="append", default=[], help="add: another address the same login is for")
    k.add_argument("--person", default=person, help=f"whose login (default {person}: [telegram] people's first)")
    v = sub.add_parser("view", help="print what the agent is shown of a page")
    v.add_argument("url")
    v.add_argument("--shot", type=Path, help="also save the screenshot it would get, refs drawn on")
    v.add_argument("--wait", type=float, default=2.0, help="seconds to let the page load first")
    r = sub.add_parser("run", help="the agent, on one goal")
    r.add_argument("goal")
    r.add_argument("--turns", type=int, default=30)
    r.add_argument("--trace", type=Path)
    r.add_argument("--really-commit", action="store_true", help="press an approved commit (default: a dry run)")
    r.add_argument("--person", default=person, help=f"whose logins it signs in with (default {person})")
    args = parser.parse_args()
    if args.command == "run":
        settings = Settings(turns=args.turns, really_commit=args.really_commit)
        you = Terminal(args.person)
        result = run_task(args.goal, you, trace_to=args.trace, settings=settings, person=you)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "vault":
        if args.action != "list" and not args.site:
            parser.error(f"vault {args.action} needs a site")
        vault_command(args)
        return
    with own_chrome(PROFILE) as address, sync_playwright() as p:
        context = p.chromium.connect_over_cdp(address).contexts[0]
        session = Session(context)
        if args.command == "signin":
            session.page.goto(args.url)
            print("Sign in by hand in that Chrome window (tick 'Keep me signed in'), then press Enter here.")
            input()
            return
        session.page.goto(args.url, wait_until="domcontentloaded")
        session.page.wait_for_timeout(args.wait * 1000)
        print(session.look().text)
        if args.shot:
            args.shot.write_bytes(session.screenshot())


if __name__ == "__main__":
    main()
