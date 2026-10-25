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
  - **Unlocking is decided by code, from what was heard.** Only by voice, only for a voice in `[home] unlock_voices`
    recognised at `unlock_threshold` or better, and only when the person's own transcribed words say "unlock"; a model
    asking to unlock for any other reason is refused (`home.py`).
  - **A Telegram chat works nothing in the house** (it can only ask who's home: `__main__.py`, `telegram()`).
  - **The browser never orders, pays, books or sends on its own.** Code checks every click and key against what
    commits (`browser/gate.py`), and presses that button only on the person's tap on Order in their own chat, and only
    if the amounts are still the ones they were shown (`browser/errands.py`, `browser/money.py`).
  - **The browser never opens an address at home** (TARS's web UI, the router, Home Assistant), however a page writes
    it or whatever a name resolves to (`homenet.py`).
  - **Secrets never reach a model.** Passwords and what a sign-in typed are left out of every page the model reads,
    covered in screenshots and blanked from traces; logins are typed by code only into their own site's origin.
- **Tests:** `tests/test_home.py::test_nothing_else_unlocks` (a note saying to let the plumber in, a typed request, an
  unsure match);
  `tests/test_browser_chrome.py::test_what_commits_is_never_clicked_whichever_way_its_asked_for`,
  `::test_a_key_on_what_commits_is_refused`; `tests/test_browser.py::test_what_commits_is_stopped`,
  `::test_a_real_approval_presses_only_on_a_yes_with_the_same_total`;
  `tests/test_browser_errands.py::test_an_order_is_approved_only_by_a_tap_on_order_in_the_persons_own_chat`;
  `tests/test_homenet.py`; `tests/test_browser_chrome.py::test_a_password_on_the_page_is_never_shown_to_the_model`,
  `tests/test_browser_login.py::test_what_a_sign_in_sent_is_blanked_out_of_the_trace`,
  `::test_origin_is_what_pages_are_compared_by`.
- **Open:**
  - A voice turn that looks something up gives OpenAI's model web search and the house tool together (`llm.py`), so a
    poisoned search result could switch a device other than the lock (the boiler, the AC). Nothing but the model
    stands in the way.
  - "A chat works nothing in the house" has no test of its own.
  - No set of injection attempts is run against the models to show these hold in practice (it would call the models,
    so it's run on request with a cost estimate).

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

- **Defenses:** none on the disk itself.
- **Open, and accepted for now:** everything is readable from the card: `.env` in plain text (nothing sets it to mode
  600 either), the browser's vault beside its key, the browser profile's cookies, every recording. Encrypting the disk
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
