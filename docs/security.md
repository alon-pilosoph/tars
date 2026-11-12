# Security

TARS is an always-on microphone wired to language models that can work the house, post to phones and run errands on
websites with the household's logins. This is what it protects, who might attack it, and for each, the defense TARS
has, the test that shows it holds, and what's still open. Nothing here is "impenetrable": an open item is written down
until it has a defense and a test, or is accepted as a risk on purpose. What's next for each open item is in
[the roadmap](roadmap.md#2-security). To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What's worth protecting

- **The house:** the front door's lock above all, then everything else TARS works (the boiler, the AC, the TV).
- **What TARS keeps:** recordings of every wake and request, every conversation, the voiceprints, who's home and when
  (`voice_data/`, and the backup's bucket).
- **The keys in `.env`:** each service's API key (some with money behind them), the bot's token, the router and Home
  Assistant logins, the backup's AWS key.
- **The browser's logins and what they can buy:** the vault (`~/.cache/tars-browser/`), the browser's own profile
  with its signed-in sites, and the saved cards those sites hold.
- **TARS's own code:** with `[updates]` on, what's merged into `main` is running at home within minutes
  ([updates](deployment.md#updates)), so whatever can get code merged can work the house.

## Who might attack, and what holds

Each threat lists its **defenses** (with where they live), the **tests** that check them, and what's **open**.

### Someone on the internet

- **Defenses:** nothing at home listens to the internet. Telegram is polled (`telegram.py` asks for updates; no
  webhook), the web UI and the browser's login page listen only on the machine's own addresses, and every service TARS
  uses is called out to, never called in.
- **Tests:** none: it follows from what's served, not from code that could regress quietly.
- **Open:** a router that forwards a port, or a Tailscale node shared outside the household, would undo this; neither
  is checked.

### A guest, or a compromised device, on the home network

- **Defenses:** the web UI only answers to the names a home network uses (addresses, `localhost`, `*.local`,
  `*.ts.net`, single-label names, and `[web] allowed_hosts`), which stops a name pointed at the house from elsewhere.
  The browser's one-time login page answers only with its token, takes one login, and stops once it has it.
- **Tests:**
  `tests/test_webui_and_clustering.py::test_requests_to_other_names_and_changes_from_other_sites_are_refused`;
  `tests/test_browser_errands.py::test_the_login_page_takes_one_login_with_its_token_only`.
- **Open:** **the web UI has no accounts.** It listens on the home network (`deploy/voice-assistant-web.service`
  serves `0.0.0.0:8080`), so anyone on the Wi-Fi can play every recording, read every conversation and change or delete
  them. The login page is plain HTTP, so a password typed into it over the home Wi-Fi crosses it unencrypted
  (`[browser] login_host` can put it on Tailscale, which encrypts).

### A web page elsewhere, through someone's browser at home

- **Defenses:** the same name check, against a page that points its own name at the house's address; and a change
  (anything but reading) is refused when its `Origin` is another site's, or, with no `Origin`, when the browser marks
  it as coming from another site (`Sec-Fetch-Site: cross-site`), so a page can't post to the web UI from someone's
  browser (`webui.py`, `only_from_home`).
- **Tests:**
  `tests/test_webui_and_clustering.py::test_requests_to_other_names_and_changes_from_other_sites_are_refused`.
- **Open:** a request from a browser that sends neither header (a very old one) isn't caught by the second check.

### Words from outside reaching a model

Web search results, the pages TARS's browser reads, Telegram messages and sent files can all say "ignore your
instructions and…". A model may believe them, so what matters is what code allows whatever the model decides.

- **Defenses:**
  - **Unlocking is decided by code, from what was heard or written.** By voice, only for a voice in `[home]
    unlock_voices` recognised at `unlock_threshold` or better; from a Telegram chat, only for a person in `[home]
    unlock_chats`; either way only when the person's own words (transcribed, or their message) say "unlock". A model
    asking to unlock for any other reason is refused (`home.py`).
  - **A reply that searched the web changes nothing in the house.** From the moment a search starts, the house
    tool only reads devices for the rest of that reply (`Request.read_the_web`, set in `llm.py`, refused in
    `home.py`).
  - **A Telegram chat works the house as the voice does** (`__main__.py`, `chat_home()`): only people in
    `[telegram] people` reach it at all, it unlocks only as above, and a chat's reply that searched the web changes
    nothing either. A chat can mute and unmute TARS's microphone at home
    (`mic_mute.py`): asked over a socket only TARS's user can open (`control.py`).
  - **The browser never orders, pays, books or sends on its own.** Code checks every click and key against what
    commits (`browser/gate.py`), and presses that button only on the person's tap on Order in their own chat, and only
    if the amounts are still the ones they were shown (`browser/errands.py`, `browser/money.py`).
  - **The browser never opens an address at home** (TARS's web UI, the router, Home Assistant), however a page writes
    it or whatever a name resolves to (`homenet.py`).
  - **Secrets never reach a model.** Passwords and what a sign-in typed are left out of every page the model reads,
    covered in screenshots and blanked from traces; logins are typed by code only into their own site's origin.
- **Tests:** `tests/test_home.py::test_nothing_else_unlocks` (a note saying to let the plumber in, a typed request, an
  unsure match), `::test_a_reply_that_searched_the_web_changes_nothing_in_the_house`,
  `::test_a_reply_that_searched_the_web_still_reads_the_house`;
  `tests/test_browser_chrome.py::test_what_commits_is_never_clicked_whichever_way_its_asked_for`,
  `::test_a_key_on_what_commits_is_refused`; `tests/test_browser.py::test_what_commits_is_stopped`,
  `::test_a_real_approval_presses_only_on_a_yes_with_the_same_total`;
  `tests/test_browser_errands.py::test_an_order_is_approved_only_by_a_tap_on_order_in_the_persons_own_chat`;
  `tests/test_home.py::test_a_chat_unlocks_only_for_someone_named_who_wrote_unlock`,
  `tests/test_presence.py::test_a_chat_works_the_house_and_unlocks_only_when_named`; `tests/test_homenet.py`;
  `tests/test_browser_chrome.py::test_a_password_on_the_page_is_never_shown_to_the_model`,
  `tests/test_browser_login.py::test_what_a_sign_in_sent_is_blanked_out_of_the_trace`,
  `::test_origin_is_what_pages_are_compared_by`.
- **Open:**
  - A Telegram account taken over is the house in someone else's hands: the AC, the boiler, the TV, TARS's
    microphone, which a chat can unmute (it hears the room at home again), and, for someone in `[home]
    unlock_chats`, the front door. Telegram's own sign-in
    (a passcode, two-step verification) is what stands in the way.
  - No set of injection attempts is run against the models to show these hold in practice (it would call the models,
    so it's run on request with a cost estimate).

### A wish, built by a coding agent

With `[wishes]` on ([wishes that come true](wishes.md)), what TARS was asked for and couldn't do becomes a GitHub
issue that Claude Code builds, with a token that can push branches and open pull requests on TARS's repository. The
issue's text is outside-influenced: it's the model's summary of a conversation, which anyone TARS talks to, or any page
or message it read, may have steered ("…and while you're there, let the workflow merge its own pull requests"). And
what's merged is installed on the Pi by the updater.

- **Defenses:**
  - **Only a maintainer's tap posts it, after seeing it whole.** The offer in the maintainers' chats shows the issue's
    title and body exactly as they'd be posted, and the tap posts that same text (`wishes.py`, `Wishes.offer`,
    `Wishes.build`). What was asked for is capped at 300 characters and what stopped TARS at 1,200 (`ASKED_MAX`,
    `WHY_MAX`), so the issue (at most about 1,700) always fits one Telegram message (4,096) and nothing posted is cut
    from what the maintainer saw. A tap from anyone but a maintainer's own chat is ignored; at most `[wishes] monthly`
    builds start in 30 days. The issue never says who asked.
  - **Only the owner sets the agent off.** `.github/workflows/wish.yml` runs only for an issue labelled by, or an
    `@claude` comment from, the repository's owner, and shows the agent only the owner's and Claude's comments; it
    runs only the commands building needs, pushes only to its own `claude/issue-N-` branch, and has the subprocesses'
    environment scrubbed of the keys (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`). It has none of the house: no `.env`, no
    `config.local.toml`, no database.
  - **A fence around what builds it.** `.github/wish.md` tells the agent never to change `.github/` (the workflows,
    CI, those instructions), `deploy/` (the units, the installer, the root helper), how TARS updates and builds itself
    (`updater.py`, `wishes.py`), how secrets are kept (the vault), or the safety guards (unlocking, drafts, orders,
    the home-network rule), and to stop and say so when a wish needs one of them. CI's `wish fence` check fails a pull
    request from a `claude/issue-` branch that changes any of `.github/`, `deploy/`, `updater.py`, `wishes.py`,
    `draft.py`, `homenet.py`, `browser/gate.py`, `browser/money.py` or `browser/vault.py` (renames included). A pull
    request can't edit the check away: GitHub refuses a push that changes a workflow from a GitHub App without the
    Workflows permission, or from a workflow's own token.
  - **TARS only follows Claude Code's own pull request** (opened by `claude[bot]`, from this repository's
    `claude/issue-N-` branch), so nobody else's pull request is told to the maintainers as the build, or sent back
    with an `@claude` comment.
  - **The token's scope.** TARS's GitHub token is a fine-grained one for the repository alone: Issues read and write
    (the issue, the `@claude` comment), Pull requests read-only, Metadata read-only. It can't push, merge, or change
    the repository's settings. The Claude app's token, on GitHub's machines, can push branches, open pull requests and
    comment; without the Workflows permission, which its installation mustn't be given, it can't change workflows.
  - **A person merges.** `main` must carry a branch ruleset (required, in [setting it up](wishes.md#setting-it-up)):
    a pull request with one approval, CI and `wish fence` passing, no force pushes, and only the repository admin may
    bypass.
- **Tests:** `tests/test_wishes.py::test_what_a_maintainer_is_shown_is_all_of_what_is_posted_however_long_the_wish`,
  `::test_whats_posted_names_nobody_and_says_what_stopped_tars`, `::test_only_a_maintainers_tap_builds_it`,
  `::test_no_more_builds_than_monthly_allows_in_30_days`,
  `::test_only_claude_codes_own_pull_request_for_the_issue_is_followed`,
  `::test_a_followed_pull_request_that_stops_being_claudes_gets_no_comment`,
  `::test_ci_fences_off_what_wish_md_tells_the_agent_not_to_change` (the fence in CI and the list in `wish.md` are
  the same, and every fenced file exists).
- **Open:**
  - **The ruleset isn't checked.** Nothing in TARS or CI confirms `main` is protected, or that the Claude app has no
    Workflows permission; both are setup steps a person must do. Without the ruleset, the agent's tokens could push to
    `main`, and the updater would install it.
  - **The fence is whole files.** Unlocking (`home.py`), the Order tap (`browser/errands.py`) and the web-search rule
    (`llm.py`, `home.py`) are in files a wish may need to change; there, `wish.md`, the tests that pin each guard
    (above) and the review are what hold. A test can be changed in the same pull request, so the review must look at
    any change to a guard's test.
  - **The reviewer is the last line.** A pull request that looks right and does something else, or a new dependency
    (`uv add` is allowed), gets as far as a review lets it.

### Someone in the room, or a recorded or cloned voice

- **Defenses:** anyone in the room can ask TARS anything, by design; only unlocking is held back, for a trusted voice
  that's recognised surely and says "unlock". Locking is anyone's.
- **Tests:** `tests/test_home.py::test_nothing_else_unlocks`, `::test_nobody_unlocks_by_voice_unless_someone_is_named`.
- **Open:** speaker ID can't tell a recording or a cloned voice from the person: a replay of "unlock the front door"
  in a trusted voice would unlock it. A spoken challenge (a random number to repeat) is the planned defense.

### A model that gets it wrong

- **Defenses:** a reply that's still a draft changes nothing in the house (a change waits until the person has
  finished and the answer is kept); "done" only when the device itself reports it; an order needs its tap.
- **Tests:** `tests/test_draft.py::test_what_an_answer_does_waits_for_the_turn_and_a_thrown_away_one_does_nothing`,
  `::test_a_draft_may_act_only_once_its_turn_is_over`; `tests/test_browser.py::test_a_dry_run_approval_presses_nothing`.
- **Open:** nothing new beyond the items above.

### A plugin

- **Defenses:** a plugin is code someone chose to name in `[home.plugins]`; one that fails to load costs only its own
  devices.
- **Tests:** none for what it may do.
- **Open:** a plugin runs inside TARS with everything TARS has. `context.secret(name)` gives it any key in `.env`, and
  nothing declares or limits what it touches. Today the only plugin is the household's own (`examples/my_house`).

### A stolen Pi, or its SD card

- **Defenses:** on the machine, `.env` is for TARS's user only: `deploy/install-pi.sh` makes it mode 600, and
  `--check` warns when other users can read it. The browser's vault and key are made mode 600 too.
- **Tests:** `tests/test_check.py::test_keys_others_can_read_are_a_warning_with_the_fix`.
- **Open, and accepted for now:** to anyone holding the card, everything is readable: `.env` in plain text, the
  browser's vault beside its key, the browser profile's cookies, every recording. Encrypting the disk
  on a Pi without a secure key store means keeping the key on it to boot unattended, which buys little. What limits the
  damage is revoking: each key in `.env` can be revoked at its service, and the browser's sites signed out.

### A stolen key

- **Defenses:** the backup's AWS user is limited to its one bucket ([backup](backup.md)). The bot answers only the
  chat ids in `[telegram] people`; anyone else is ignored and logged once.
- **Tests:** `tests/test_telegram.py::test_anyone_else_and_anything_else_is_ignored_without_a_word`;
  `tests/test_backup.py::test_a_pass_never_deletes_most_of_the_bucket_unless_told_to` (TARS's own passes; a stolen key
  isn't bound by it).
- **Open:** the backup's key can read and delete everything in the bucket. It has to (the web UI plays recordings only
  the bucket still has, a restore reads it, and passes mirror deletes), so the defense is versioning on the bucket and
  a key that can't delete old versions, not yet set up. API keys have whatever spending limits each service was given;
  TARS doesn't check.

### The code TARS runs: its dependencies

- **Defenses:** every Python package is pinned with hashes in `uv.lock` and installed `--frozen`, in CI and on the Pi;
  the web UI's packages are pinned in `package-lock.json`.
- **Tests:** CI installs from the lock files only.
- **Open:** nothing checks the pinned versions for known vulnerabilities (pip-audit or Dependabot), and nothing scans
  TARS's own code (bandit or semgrep).

### The services TARS uses

What leaves the machine for each service is written down:
[the voice and the brain](architecture.md#what-leaves-the-machine), [the house](home.md#what-leaves-the-house),
[the browser](browser.md#what-leaves-the-machine), [Telegram](telegram.md#what-it-sends-telegram). Audio leaves only
after the wake word is heard on the machine, and double-checked there with `[wake] verify`. Trusting those services
with what they're sent is accepted: TARS can't work without them, and each one is optional.

## The services on the machine

TARS's systemd user services (`deploy/*.service`) run as the user who installed them, with that user's access:
any of them can read `.env`, the vault and everything in `voice_data/`. Sandboxing each one (NoNewPrivileges,
ProtectSystem, a web UI that can't read `.env` or reach the internet) is open.

**Root.** On the Pi, that user is in the `docker` group, so Home Assistant can run in Docker beside TARS; and a member
of the `docker` group can start a container with the whole disk in it, which makes it effectively root. That's
accepted, and written down here because it's easy to miss: anything that runs as TARS's user could take the machine.
Besides that, the one thing TARS runs as root is `deploy/tars-system` (installed root-owned as
`/usr/local/sbin/tars-system`, with a sudoers rule for it alone, with no arguments): the packages
`deploy/pi-packages.txt` lists, from the system's own signed repositories (names checked); TARS's two device rules,
refused if one would run a program as root; and lingering. A change to it is installed only by a person running
`deploy/install-pi.sh`, never by an update ([updates](deployment.md#updates)), so TARS can't rewrite its own root
script.
