# Security

TARS is an always-on microphone wired to language models that can work the house, post to phones and run errands on
websites with the household's logins. This is what it protects, who might attack it, and for each, the defense TARS
has, the test that shows it holds, and what's still open. Nothing here is "impenetrable": an open item is written down
until it has a defense and a test, or is accepted as a risk on purpose. What's next for each open item is in
[the roadmap](roadmap.md#2-security). To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What's worth protecting

- **The house:** the front door's lock above all (TARS works it by itself, with auto-lock), then everything else
  TARS works (the boiler, the AC, the TV).
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
- **Defense, with `[web] sign_in`:** every page and API needs a session made with a passkey, from a one-time invite;
  the session cookie is HttpOnly, SameSite=Strict and Secure over HTTPS (served with `[web] tls_cert`, a Tailscale
  certificate) ([signing in](web-ui.md#signing-in)). After signing in, `?next=` goes back to a page of TARS only,
  never another site or a `javascript:` link. **Test:** `tests/test_signin.py` walks every route without a session
  and fails if one answers, if a websocket lets anyone in (the session check is HTTP middleware, which websockets
  skip), or if it meets a kind of route it can't ask; and refuses a passkey answer for another origin, a replayed
  challenge, an unknown passkey and a used or expired invite. `webui/checks/signin.spec.ts` signs in end to end in
  Chromium with a virtual authenticator, `?next=/\evil.example`, `//evil.example`, another site and `javascript:`
  included.
- **Open:** **sign-in is off until it's turned on**, since it needs HTTPS first. Until then the web UI listens on the
  home network (`deploy/voice-assistant-web.service` serves `0.0.0.0:8080`), so anyone on the Wi-Fi can play every
  recording, read every conversation and change or delete them; `--check` warns while it's off. The login page is
  plain HTTP, so a password typed into it over the home Wi-Fi crosses it unencrypted (`[browser] login_host` can put
  it on Tailscale, which encrypts).

### A device on the home network, speaking for the front door

The front door's Nuki says who opened it by publishing to the household's MQTT broker, and TARS takes what the
broker hands it on `<topic>/lockActionEvent` as the lock's word ([the front door](presence.md#the-front-door),
`doors.py`). Whoever can publish there speaks for the lock.

- **What a forged message could do:** put a named person "home", from outside, before their phone: "who's home?"
  says so, a reminder meant for them while they're out is said aloud at home instead of going to their chat, and a
  message waiting for them is said aloud at home, to whoever is there. It sets off the skills waiting for someone to
  come home, and adds false rows to `door_events`. A forged opening by a key nobody's named, as someone's real phone
  comes home, counts towards that key being learned as theirs (two separate arrivals, and never with anyone else's
  phone, before it is).
- **What it can't do:** unlock, or change anything else. A skill a door's lock sets off reads the house but changes
  nothing in it: every change is refused for that run, an unlock first (`Request.by_door`, refused in `home.py`,
  `ha_tool.py`, `ha_setup.py`, for a plugin's own tools in `toolbox.py`, and for TARS's own settings in `myself.py`),
  and an unlock needs a trusted voice
  saying "unlock" anyway. TARS publishes nothing to the broker, so nothing it hears there goes back to the lock. A
  lock never marks anyone out: leaving is still by the phone, hurried only by a door sensor
  ([below](#a-door-sensor-that-says-the-door-moved-when-it-didnt)).
- **Defenses:**
  - The broker lets in only users with a password (`allow_anonymous false`, `password_file`), and its ACL
    ([`deploy/home-assistant/mosquitto/acl`](../deploy/home-assistant/mosquitto/acl)) refuses anything not granted:
    only the lock's own user (`nuki`) and Home Assistant, which works the lock anyway, may write the locks' topics; any
    other device writes only under its own name; TARS may only read the locks' events.
  - TARS signs in as its own user, its password in `.env` under `[presence.mqtt] secret`: doors without `user` and
    `secret` are a mistake in the config. `deploy/install-pi.sh` gives TARS that user on the Mosquitto here (a random
    password, written into Mosquitto's passwords through its own image) and has Mosquitto read the ACL
    (`mosquitto.py`).
  - `--check` writes beside a lock's events (`<topic>/tars-check`, a topic the lock ignores) and warns if it comes
    back: a broker that lets TARS write there has no ACL keeping the lock's topics to the lock.
  - What the broker kept from before (a retained message) is never an arrival, and only a payload of whole numbers
    in Nuki's order is read at all.
- **Tests:** `tests/test_home.py::test_a_turn_a_doors_lock_set_off_reads_the_house_and_changes_nothing_in_it`;
  `tests/test_ha_tool.py::test_a_turn_a_doors_lock_set_off_reads_but_changes_nothing`;
  `tests/test_toolbox.py::test_a_tool_that_changes_something_is_refused_on_a_turn_a_doors_lock_set_off_and_reads_still_go`,
  `::test_what_holds_a_change_back_is_the_web_then_a_door_then_the_turn`;
  `tests/test_ha_setup.py::test_someone_else_cant_confirm_a_removal_and_only_maintainers_may_change_it`;
  `tests/test_settings.py` (the settings tool's refusals);
  `tests/test_skills.py::test_a_skill_a_doors_lock_set_off_runs_reading_the_house_only`,
  `tests/test_telegram.py::test_a_chat_skill_a_doors_lock_set_off_runs_reading_the_house_only`;
  `tests/test_doors.py::test_doors_need_tars_to_sign_in_to_the_broker_as_itself`,
  `::test_check_says_whether_the_broker_lets_tars_in_and_keeps_the_locks_topics_to_the_locks`,
  `::test_listening_subscribes_to_each_doors_lock_and_hands_on_what_it_says` (an old, retained event is ignored),
  `::test_anything_that_isnt_a_lock_action_is_ignored`; `tests/test_mosquitto.py`. The ACL itself was tried against
  Mosquitto 2.1 in Docker: the lock's event heard, the same event from another device's user and from TARS's dropped.
- **Open:**
  - **MQTT is plain on the home network.** Port 1883 has no TLS, so a device that can see the Wi-Fi's traffic (one
    that's taken over, spoofing the router's address) reads each password as it's sent, the lock's included, and can
    then publish as the lock. TLS on the broker, and on the lock where the Nuki model has it, is the planned defense.
  - Home Assistant's user may write everything, the locks' topics included: a Home Assistant taken over could say
    anyone came home (it could unlock the door anyway).
  - Two locks on one user (`nuki`) can each speak for the other; the Nuki's events aren't signed, so nothing past the
    broker tells a forged one from a real one.
  - A broker on another machine is set up by hand: `--check`'s warning is all TARS checks there.

### A door sensor that says the door moved when it didn't

A door with a sensor ([the door sensor](presence.md#the-door-sensor)) is followed in Home Assistant: TARS takes the
binary_sensor's state there as the door opening and shutting (`hass.follow`, `doors.Doors.sensed`). Whoever can set
that state, or what Home Assistant hears from the sensor, speaks for the door:

- anyone with a Home Assistant login or token allowed to set states (an administrator's), and any automation,
  script or integration in Home Assistant;
- with the sensor's BTHome encryption off, anyone in Bluetooth range: an unencrypted BTHome advertisement isn't
  signed, so a phone or an ESP32 nearby can send one as the sensor.

- **What a forged "open" or "shut" could do:**
  - hurry the routers: asked every 10 seconds instead of every 30, for at most a quarter of any hour however often
    it says so;
  - mark someone out early, but only someone whose phone really did drop off the Wi-Fi from a round before the
    forged opening to a minute after it, and stayed off for a minute and a half. That person is out, and the skills
    waiting for them (or for the last one out) to go out run as for anyone going out, changes to the house included;
    they're home again the moment their phone is seen;
  - with a real let-in by the lock, have them "come in" sooner (a forged shut) or not at all (no shut), and keep a
    real opening from inside within a minute of it from being taken for someone leaving.
- **What it can't do:** put anyone at home (only a router seeing their phone, or the lock letting them in, does);
  welcome anyone the lock didn't let in from outside within two minutes; change anything in the house on someone
  coming in (that run is the door's, `Request.by_door`, as for the lock above); unlock. A sensor going unavailable
  and coming back, or Home Assistant restarting, isn't a move at all.
- **Defenses:**
  - Leaving by the door asks for the phone too, and only a phone there as the door opened
    (`presence.Tracker._left_by_door`); someone known only by the lock letting them in, or let in just before, is
    never out by it.
  - Only a change from open to shut, or back, is a move (`hass.follow`'s `moves_only`).
  - The routers' hurrying is bounded (`presence.Watcher._round_due`), and the door uses kept for leaving are the
    newest 16.
  - Turn on the sensor's BTHome encryption (a bindkey), as the [setup](presence.md#the-door-sensor) says: then only
    something with the key can speak as it over Bluetooth.
- **Tests:** `tests/test_presence.py::test_a_phone_already_dozing_when_the_door_is_opened_from_inside_isnt_leaving_by_it`,
  `::test_someone_elses_phone_dozing_a_while_after_the_door_isnt_leaving_by_it`,
  `::test_someone_known_only_by_the_lock_letting_them_in_is_never_out_by_the_door`,
  `::test_a_door_that_never_stops_moving_hurries_the_routers_for_a_quarter_of_each_hour_at_most`;
  `tests/test_doors.py::test_the_door_opened_with_nobody_let_in_is_someone_inside_using_it_and_nobody_is_welcomed`,
  `::test_the_door_bouncing_or_opened_again_just_after_someone_came_in_isnt_anyone_leaving`,
  `::test_the_door_sensors_are_followed_for_their_moves_only`;
  `tests/test_hass.py::test_following_moves_only_leaves_out_a_change_from_unknown_or_unavailable`;
  `tests/test_skills.py::test_someone_coming_in_by_the_door_sets_off_the_skills_waiting_for_them_as_by_the_door`
  (a came-in run carries its door, for which `home.py` refuses every change).
- **Open:**
  - TARS can't tell whether the sensor's BTHome encryption is on: Home Assistant doesn't say. `--check` reads the
    sensor, not how it's paired.
  - With encryption on, a recorded advertisement replayed is still for Home Assistant's BTHome integration to
    refuse; that hasn't been tried here.
  - Home Assistant's administrators, and its automations, can set the sensor's state as they can work the lock.

### Auto-lock: TARS working the front door's lock on its own

With `auto_lock` on a lock ([auto-lock](home.md#auto-lock), `autolock.py`), TARS locks the front door by itself,
from what Home Assistant says of the lock and its door sensor: the one thing it does to the lock that nobody asked
for. Whoever can make Home Assistant say the door is shut, or open, steers it (as for
[the door sensor](#a-door-sensor-that-says-the-door-moved-when-it-didnt) above).

- **It only ever locks.** Its one call is `lock.lock`; nothing it's given can unlock, unlatch or open, whatever it's
  told. At worst it locks the door, or leaves it unlocked.
- **What a forged "shut" could do:** with the door open and unlocked, get it locked: the bolt thrown into the open
  door, out against the frame when someone shuts it. Nobody is let in by that, but the door won't shut until the
  bolt is turned back by hand or in the app.
- **What a forged "open" could do,** or a sensor kept unavailable: keep TARS from locking. The door stays unlocked
  until someone locks it or the lock's own timer does; kept unavailable, the maintainers are told after a minute.
- **The sensor down:** nothing is locked until it's back, whatever it said last; the maintainers are told once it's
  been out a minute, and once it's back, and the lock's own auto-lock is the backstop. A read just before locking
  that Home Assistant doesn't answer is tried three times, then told; it's never taken as shut.
- **Turning it off:** any voice in the room, or any household chat, can turn it off for up to 12 hours with the home
  tool's `<door> auto-lock`. That's held back by the guards every change has (a draft thrown away, a reply that read
  the web, a run a door's lock set off: `home.py`'s `_change` and the device's own `set_off_by_door`), but not by
  the trusted-voice rule unlocking has. That's on purpose: off unlocks nothing, it only leaves the door as it is,
  and anyone who can speak to TARS is inside, where the thumb-turn leaves the door unlocked just as well. Off comes
  back on by itself, at most 12 hours later, and the lock's own timer still locks meanwhile. A trusted voice for it
  would make stepping out to the bins (the [lockout](home.md#auto-lock)) a three-digit challenge for the guest or
  the child who needs it, for no door kept safer.
- **Defenses:** the rule above (lock only on a sensor read "shut" just before, `AutoLock._read_and_lock`); a
  sensor out, or Home Assistant not answering, never counts as shut; the lock and the door read again over REST each
  time following is taken up and every 30 s, so a change missed isn't acted on as it was; a read never undoes a
  newer change; only locked to unlocked is a new unlocking, so a flapping lock isn't tried again and again; 3 s at
  least between the door shutting and the bolt. Turn on the sensor's BTHome encryption (a bindkey), as the
  [door sensor's setup](presence.md#the-door-sensor) says: then only something with the key can say "shut" for it
  over Bluetooth.
- **Tests:** `tests/test_autolock.py::test_a_sensor_gone_quiet_while_the_door_is_open_never_gets_it_locked`,
  `::test_the_door_read_as_anything_but_shut_just_before_locking_isnt_locked`,
  `::test_a_door_that_cant_be_read_just_before_locking_isnt_locked_and_is_tried_again_then_told`,
  `::test_a_lock_dropping_out_of_reach_and_back_isnt_a_new_unlocking`,
  `::test_a_read_again_never_undoes_a_newer_change`, `::test_auto_lock_can_only_lock`,
  `::test_a_door_opened_between_the_read_and_the_bolt_still_gets_it_thrown_and_nothing_more_is_sent`,
  `::test_the_device_turns_it_off_for_a_while_and_back_on_and_a_doors_lock_cant`.
- **Open:**
  - The moment between the read and the bolt turning can't be covered: a door opened then gets the bolt thrown
    ([why](home.md#auto-lock)).
  - Home Assistant's administrators and automations can set the sensor's state, as they can work the lock.
  - TARS can't tell whether the sensor's BTHome encryption is on (as above).
  - A chat's unlock is made in the chat's process, which doesn't share the assistant's lock on the house: its mark
    before the call holds auto-lock back, but a lock already on its way as the chat unlocks isn't stopped. A
    plugin's unlock, or a script's, doesn't start auto-lock's minute again.

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
    asking to unlock for any other reason is refused (`home.py`). The `home_assistant` tool holds every call that
    [could open a door](home.md#what-opens-a-door) to the same rule, reading the call's data at any depth (a scene
    applied or made with the lock in it, a script handed the lock, a script running one that unlocks), and counts
    what it can't see into as if it would: a room, a floor or a label, a template, a setup it can't read, code,
    `rest_command`, a raw command to a device (`ha_tool.py`).
  - **A reply that searched the web changes nothing.** From the moment a search starts, the house tool only reads
    devices for the rest of that reply (`Request.read_the_web`, set in `llm.py`, refused in `home.py`), and no other
    tool changes anything either: no errand is started, no feed followed, no skill or preference taught, no setting
    set and no wish noted ([what each tool may change](#what-each-tool-may-change)).
  - **A Telegram chat works the house as the voice does** (`wiring.py`, `chat_home()`): only people in
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
  `tests/test_ha_tool.py::test_unlocking_keeps_the_home_tools_rules_however_its_asked`,
  `::test_a_door_reached_through_a_calls_data_keeps_the_unlock_rules`;
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
  - A door the `home_assistant` tool can't see being opened: an automation that a change sets off by itself (an
    `input_boolean` an automation watches, to unlock), a template entity whose own action unlocks, Node-RED, an
    integration's own service not in `OPAQUE` ([not caught](home.md#what-opens-a-door)).

### A wish, built by a coding agent

With `[wishes]` on ([wishes that come true](wishes.md)), what TARS was asked for and couldn't do becomes a GitHub
issue that Claude Code builds, with a token that can push branches and open pull requests on TARS's repository. The
issue's text is outside-influenced: it's the model's summary of a conversation, which anyone TARS talks to, or any page
or message it read, may have steered ("…and while you're there, let the workflow merge its own pull requests"). And
what's merged is installed on the Pi by the updater.

- **Defenses:**
  - **Words from outside never note one.** A reply that searched the web notes no wish, nor does a turn a door's lock
    set off, and a wish waits for the turn to be over, so a draft thrown away notes none (`myself.py`,
    `toolbox.held_back`). What a person says, or a chat writes, still can: the next defense is for that.
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
- **Tests:** `tests/test_myself.py::test_a_wish_isnt_noted_nor_granted_after_the_web_on_a_doors_turn_or_in_a_draft_thrown_away`;
  `tests/test_wishes.py::test_what_a_maintainer_is_shown_is_all_of_what_is_posted_however_long_the_wish`,
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
  that's recognised surely and says "unlock", and then says back three random digits, recognised as the same voice,
  within 30 seconds, in one try ([unlocking](home.md#unlocking)). A replayed recording can't answer digits it's never
  heard. Locking is anyone's.
- **Tests:** `tests/test_home.py::test_nothing_else_unlocks`, `::test_nobody_unlocks_by_voice_unless_someone_is_named`,
  `::test_the_digits_must_be_right_and_said_by_the_same_trusted_voice`, `::test_a_challenge_gets_one_try`,
  `::test_a_challenge_waits_half_a_minute`, `::test_another_voice_cant_answer_or_use_up_someones_challenge`.
- **Open:** a voice cloned live, by someone who hears the digits and can make the clone say them within half a
  minute, still gets through; so does a recording that happens to hold the right digits in that voice. The challenge
  raises the cost from replaying one sentence to running a clone in the room.

### A model that gets it wrong

- **Defenses:** a reply that's still a draft changes nothing in the house (a change waits until the person has
  finished and the answer is kept); "done" only when the device itself reports it; an order needs its tap.
- **Tests:** `tests/test_draft.py::test_what_an_answer_does_waits_for_the_turn_and_a_thrown_away_one_does_nothing`,
  `::test_a_draft_may_act_only_once_its_turn_is_over`; `tests/test_browser.py::test_a_dry_run_approval_presses_nothing`.
- **Open:** nothing new beyond the items above.

### A plugin

- **Defenses:** a plugin is code someone chose to name in `[home.plugins]`, and declares what it needs; with `grant`
  set, one that needs more doesn't load, and its context holds only what was granted: other keys in `.env` are
  refused, Home Assistant, speaking and telling are absent unless granted. It gets Home Assistant's reading methods,
  never the client, and each of its calls is checked against each grant on its own, so neither brings the other:
  - without `unlock`, Home Assistant refuses whatever [could open a door](home.md#what-opens-a-door) by the same
    check as the `home_assistant` tool, read at any depth of the call's data (a lock or its unlatch button, a scene
    applied or made with the lock in it, a group or a device with it, a script, automation or scene that would unlock
    or runs one that would), and whatever can't be seen into (a room, a floor or a label, a template, a setup that
    can't be read, code, `rest_command`, a raw command to a device); opening a garage, door or gate, and disarming,
    too;
  - without `ha_admin`, it never administers Home Assistant (`ADMIN`); running code there needs both.

  The other devices are read-only views that unlock only with `unlock`, and then only as the home tool allows. A
  plugin tool that changes something waits for the turn, isn't called by a reply that read the web nor in a turn a
  door's lock set off, and can't take another tool's name ([plugins](home.md#plugins)).
- **Tests:** `tests/test_plugins.py`: a plugin needing more than its grant doesn't load; an ungranted key is refused;
  `::test_each_grant_is_checked_on_its_own` (every combination of `unlock` and `ha_admin` against an unlock, an
  administering, an ordinary and a both-needing call); every route that opens the house is refused without `unlock`
  (`::test_whatever_could_reach_the_lock_however_its_nested_needs_the_unlock_grant`: each nested shape, the scene
  applied or made, the room, the template, the service that can't be seen into),
  `::test_what_cant_be_read_counts_as_opening_the_door`, and a service is only ever a plain name; the client and its
  setup aren't handed over; another device can't be unlocked through its view; a changing tool waits for the turn and
  refuses a reply that read the web and a turn a door's lock set off; `--check` warns on a grant of TARS's own keys.
- **Open:** grants hold at the interface, not in a sandbox: a plugin is Python in TARS's process and could reach
  around them, and every plugin keeps `root`, so it can read the project's files, `.env` included. One is installed
  only after being read. A plugin with no `grant` set loads with everything (as before grants), with a `--check`
  warning. Network access isn't limited. What the `home_assistant` tool doesn't catch, a plugin's Home Assistant
  doesn't either: an automation a change sets off by itself, a template entity whose own action unlocks, anything
  outside Home Assistant listening for a change, an integration's own service not in `OPAQUE`
  ([not caught](home.md#what-opens-a-door)).

### A stolen Pi, or its SD card

- **Defenses:** on the machine, `.env` is for TARS's user only: `deploy/install-pi.sh` makes it mode 600, and
  `--check` warns when other users can read it. The browser's vault and key are made mode 600 too.
- **Tests:** `tests/test_check.py::test_keys_others_can_read_are_a_warning_with_the_fix`.
- **Open, and accepted for now:** to anyone holding the card, everything is readable: `.env` in plain text, the
  browser's vault beside its key, the browser profile's cookies, every recording. Encrypting the disk
  on a Pi without a secure key store means keeping the key on it to boot unattended, which buys little. What limits the
  damage is revoking: each key in `.env` can be revoked at its service, and the browser's sites signed out.

### The browser's Sprite, or Fly

With `[browser] sprite`, the browser runs on a Fly Sprite ([on a Sprite](browser.md#on-a-sprite)). Whoever takes it
over, or Fly itself, has what the browser has while it runs: the pages, each person's cookies, and the logins as
they're typed. What else they could reach is what this section is about.

- **Defenses:**
  - *The home network.* The Sprite's only way out is the link back to the way out on this machine
    (`browser/way_out.py`), which refuses home names, the names TARS is set up with, every address that isn't
    public (an IPv4 address written as IPv6 included), and every port but 80 and 443. It resolves a name once,
    checks every address, and connects to the one it checked, so a name can't answer differently the second time.
    The Sprite's own localhost is sent there too (`<-loopback>`), and refused.
  - *Everything else.* The Sprite's network policy denies every name and address. It's set from outside through
    Fly's API and can't be changed from inside, so a Sprite taken over still reaches only the way out.
  - *The link.* It's opened from here, through `sprite proxy`; the Sprite has no port on the internet. The Sprite's
    sshd listens on its localhost only and accepts one key, which may forward Chrome's port and the control
    service's and offer the way out, and nothing else: no command (`command=`, `ForceCommand`), no file transfer, no
    other forward. This machine checks the Sprite's host key against the one `setup.sh` recorded.
  - *Chrome's debugging port,* which has no password and gives whoever reaches it every signed-in account in the
    profile, listens on the Sprite's localhost only, reached from here through the link.
  - *The control service* listens on the Sprite's localhost, wants a key besides, and takes only a profile name made
    of letters, digits and dashes.
  - *The vault* never goes to the Sprite: logins are typed through the link, as they're typed into a local Chrome.
  - *The Sprite's name* goes into ssh's `ProxyCommand`, which a shell runs: it's held to lower-case letters, digits
    and dashes in the config check and again where the command is made.
  - *Its public address,* for [handing over](browser.md#handing-over), reaches only the control service's gate.
    Without a hand-over on, it answers 404 to everything and passes nothing on. During one, it lets in the browser
    that brings that hand-over's token (32 random bytes, sent only in the person's chat), one browser at a time,
    through a cookie made for that browser (`Secure`, `HttpOnly`, `SameSite=Strict`): the newest to open the link
    takes over, and the last one's connections are closed. Every request is checked: each goes to Guacamole on a
    connection of its own (`Connection: close`; the screen's WebSocket is its own connection anyway), since Fly's
    proxy reuses a connection to the Sprite for whoever comes next, and a check of a connection's first request
    alone let a request without a cookie through on one the person had opened (found on the real Sprite, fixed,
    and tested). Behind the gate, Guacamole asks for its own login (user `tars`, a random password made by
    `setup.sh`, sent with each link). Guacamole (Tomcat, with no shutdown port and none of its sample apps), guacd
    and the VNC server (x11vnc, without a password: the gate and Guacamole are the locks) listen on the Sprite's
    localhost only. When the hand-over ends (Done, Give up, 15 minutes, or the errand's end), the gate closes every
    connection. TARS says in the chat each time the screen is opened.
- **Tests:** `tests/test_browser_way_out.py` (each refusal, then real SOCKS5 conversations: home refused on the wire,
  the checked address is the one connected to); `tests/test_browser_sprite.py` (the control service's key and profile
  names, the link forwarding three ports and running nothing, a Sprite's name that would reach the shell). By hand on
  a real Sprite: a command, sftp and an unlisted forward refused; with the policy on, the Sprite's own requests fail by
  name and by address, while the browser loads pages from the home's address and is refused the router, the Sprite's
  localhost and its own debugging port. The gate: `tests/test_browser_sprite.py` (nothing without a hand-over, the
  token's browser in and no guess, a request riding on an allowed connection never reaching Guacamole, a second
  opening taking over and closing the first, the end of one). Over the real public address: 404 to everything with
  no hand-over; during one, the link's browser in, Guacamole's login with its password and not with a wrong one,
  a guessed cookie and none refused five times out of five, a second browser taking over, 404 after; a phone-sized
  browser logging in and seeing the screen.
- **Open:** whoever takes the Sprite over can browse the public web from the home's address on ports 80 and 443 while
  a link is open, as the browser itself can. The Fly token on this machine can reach the Sprite (run commands on it,
  read the profiles): it's guarded like the vault's key, in TARS's user's own folder. Fly, which runs it, is trusted
  with what the browser sees. During a hand-over, whoever holds the link (a forwarded message, the person's unlocked
  phone) can work the browser as the person, orders included, until it ends; the chat says when it's opened. Anyone
  can wake the Sprite for a moment by asking its address, which costs seconds of its time. The gate is on the
  internet all the time, Guacamole through it during a hand-over: a flaw in the gate, or in Guacamole or Tomcat for
  whoever holds the link, would be reachable. Guacamole's password is the same for every hand-over until `setup.sh`
  makes a new one (delete `sprite-viewer-password` first), and it's in every link's message. Guacamole and Tomcat are
  pinned versions, updated by editing `install-viewer.sh` and running `setup.sh` again.

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
- **Checked on every push** (CI's `pip-audit`, `npm-audit`, `bandit` and `sbom` jobs, each on its own so one failing
  doesn't hide the others): pip-audit against every locked Python package (all extras), `npm audit` against the web
  UI's runtime packages (high and above), and bandit over TARS's own code, `examples/` and `tools/` (medium and above).
  The tools themselves are pinned in `uv.lock` (pyproject's `security` group). A known vulnerability, or a new finding,
  fails CI. Each run keeps CycloneDX bills of materials of the Python packages (`sbom.json`) and the web UI's runtime
  packages (`sbom-webui.json`). Feeds and the TV's screen dumps are read with `defusedxml` (XML from outside: no
  entities, no expansion bombs). The local models download only over HTTPS, refusing a redirect off it, from addresses
  pinned to a release or revision, and each is kept only if its SHA-256 is the one in the code. Bandit's false
  positives (fixed table and column names in SQL, Chrome's debugging port on localhost) are marked one by one with
  `# nosec` and why, never skipped wholesale.
- **Tests:** CI installs from the lock files only, and its security jobs fail on a known vulnerability or finding.
- **Open:** a vulnerability published after a release is only caught on the next push (no scheduled audit), and
  releases aren't signed yet (roadmap step 4).

### The services TARS uses

What leaves the machine for each service is written down:
[the voice and the brain](architecture.md#what-leaves-the-machine), [the house](home.md#what-leaves-the-house),
[the browser](browser.md#what-leaves-the-machine), [Telegram](telegram.md#what-it-sends-telegram). Audio leaves only
after the wake word is heard on the machine, and double-checked there with `[wake] verify`; with `[home]
local_first`, only once the request is over and isn't a house command, which Vosk hears on the machine
([what leaves the machine](architecture.md#what-leaves-the-machine)). Besides, a house recording session made
with `--cloud` has its takes sent to Deepgram when they're measured, which its page says before anything is
recorded; any other session's takes stay on the machine ([recording sessions](web-ui.md#recording-sessions)).
Trusting those services with what they're sent is accepted: TARS can't work without them, and each one is optional.

## What each tool may change

Every tool the models are offered says what calling it may change (`toolbox.py`), and the guards below are in code,
whatever the model decides. "The turn" is the draft rule: a change waits until the person has finished and the reply
is kept (`Request.go_ahead`), so one thrown away, because they kept talking, changes nothing. "The web" is a reply
that searched it (`Request.read_the_web`): from the search on, its pages may be steering the model. "A door" is a
skill's own turn that a door's lock set off (`Request.by_door`): a lock only ever says who came in. A `CHANGES` tool
is held to all three by the toolbox itself (`toolbox.held_back`); an `ITS_OWN` tool tells its reads from its changes
and holds each change at least as `held_back` does. Reading is never held back, except where the table says.

| Tool | What it may change | Waits for the turn | Refused after the web | Refused on a door's turn | Also held by |
|---|---|---|---|---|---|
| `home` | a listed device: on, off, set, press, lock, unlock, open, close, play, the TV agent's errand, a camera's picture or link | yes | yes | yes | unlocking only for a trusted voice, recognised surely, or a trusted chat, saying "unlock", then the three digits; "done" only when the device reports it; one change at a time |
| `home_assistant` | any entity, through a service; and Home Assistant's setup (add, rename, room, remove, undo) | yes | yes; nor is the house's past read (history, statistics, logbook) | yes | a call that could open a door, as unlocking; no administering Home Assistant; the setup only for maintainers, never a secret through a chat, removing or undoing on a second message |
| a plugin's tool | what the plugin's code does, when it declares `changes=True`; one that doesn't is called as a read | yes | yes | yes | its grants; one change at a time with the house's |
| `browse` | an errand queued, run in the person's own accounts | yes | yes | yes | only for someone known with a chat; nothing ordered, booked, paid or sent without their tap on Order, at the amounts shown |
| `feeds` | a feed followed or dropped (reading marks what was told) | yes | yes | yes | each person's own |
| `skills` | a skill saved, changed, paused, resumed or deleted; a preference remembered or forgotten | yes | yes | yes | only someone TARS knows; a skill does each step through the tools above, under their rules |
| `tars` | a setting set (`config.local.toml`, then a restart); a wish noted (to the maintainers, maybe a GitHub issue) or granted | yes | yes; nor are the records or the log read | yes | a setting only for someone known or asking aloud, only what `ADJUSTABLE` lists, within bounds; granting only for maintainers; a wish's issue posted only on a maintainer's tap |
| `send` | an item on the TARS page and the asker's chat | made only once the reply is kept | no: links come from the web | no | a link must be http(s), a file a known type |
| `remind`, `cancel_reminder`, `snooze_reminder` | a reminder, timer or message set, cancelled or put off | made only once the reply is kept | no | no | checked when asked; for whom, by the names TARS knows |
| web search | nothing here: it's OpenAI's, run on its side | | | | marks the reply as having read the web, for every tool above |

`send` and the reminders only put what they make on the turn (`COLLECTS`), so a reply that's dropped or skipped makes
none of it; a skill's own turn makes it once its line is said or posted (or, a one-off done, handed on to be told),
and not when it was skipped, didn't happen, or nobody heard it. They aren't refused after the web or on a door's turn: what they make is said or shown to the household,
and changes nothing in the house.

- **Tests:** `tests/test_toolbox.py` (the toolbox's guards on a `CHANGES` tool, and `held_back`'s order);
  `tests/test_plugins.py::test_a_plugins_tools_reach_the_brain_and_one_that_changes_waits_for_the_turn`;
  `tests/test_home.py`, `tests/test_ha_tool.py`, `tests/test_ha_setup.py` (above);
  `tests/test_browser_errands.py::test_no_errand_is_started_in_a_reply_that_read_the_web_nor_in_a_turn_a_doors_lock_set_off`,
  `::test_an_errand_needs_someone_known_asking_a_goal_and_a_turn_that_goes_ahead`;
  `tests/test_feeds.py::test_following_waits_for_the_turn_and_isnt_done_after_the_web_or_on_a_doors_turn_but_reading_is`;
  `tests/test_skills.py::test_nothing_is_taught_or_forgotten_after_the_web_or_on_a_doors_turn_but_its_still_told`,
  `::test_a_turn_thrown_away_teaches_nothing`;
  `tests/test_myself.py::test_a_wish_isnt_noted_nor_granted_after_the_web_on_a_doors_turn_or_in_a_draft_thrown_away`;
  `tests/test_settings.py::test_the_tars_tool_changes_one_only_for_someone_known_or_at_home_and_never_after_a_search`.
- **Open:** a reminder or a message can be set in a reply that read the web, so a page could word one ("remind
  Stacey to call this number"); it's only ever said, or posted, to the household.

## The services on the machine

TARS's systemd user services (`deploy/*.service`) run as the user who installed them, and each is sandboxed:

| Service | What it may do | Exposure (`systemd-analyze security`) |
|---|---|---|
| The voice assistant | write only its own folder and caches; a `/tmp` of its own; files it makes readable by its user alone (`UMask=0077`); no new privileges (no `sudo`); local, IPv4, IPv6 and netlink sockets only; no kernel tunables, modules, logs, cgroups, clock or hostname; no capabilities; system calls a service makes, and no others. Keeps the sound card, the IR remote's input device and Bluetooth (over D-Bus) | 9.6 → 3.4 |
| The web UI | the same, and no Docker. Keeps the devices: the Status page's power and heat warnings come from the Pi's firmware, which `vcgencmd` asks through `/dev/vcio` | 9.6 → 3.4 |
| The backup | the same, and no devices at all. It can't reach the user's service manager | 9.6 → 3.1 |
| Telegram | the first half: its own folder and Chromium's two folders under `~/.config`, its own `/tmp`, files for its user alone, no new privileges, those sockets. Chromium's own sandbox, for errands, needs the namespaces and system calls the rest would take away | 9.6 → 8.3 |
| The browser's screen (Xvfb) | writes only to `/tmp` (its lock and its socket), no home (so no service manager either), no Docker, no network but local sockets, no devices, the same kernel and system-call limits | 9.6 → 2.9 |
| The updater | not sandboxed: it runs `deploy/tars-system` through `sudo` and rewrites the checkout and these units | unchanged |

Netlink is allowed because glibc asks the kernel for the machine's own addresses over it to order the answers when it
looks a name up, and Chromium watches the network over it. The browser, in Telegram's `/tmp` of its own, can't see the
screen's socket file: it reaches the screen over Xvfb's abstract socket, which belongs to the network namespace, so
`PrivateNetwork=` on either unit would cut it off.

The web UI isn't given just `/dev/vcio` (`DevicePolicy=closed` with `DeviceAllow=/dev/vcio`): systemd enforces those
with a cgroup BPF program that only a service manager running as root loads, so in a user unit they'd do nothing. A
Pi whose warnings can't be read, a sandbox that hides the device included, says so on the Status page and in the log
rather than showing nothing, as if it weren't a Pi.

How it was checked: on the Pi (systemd 257), `voice-assistant --check` run inside the voice assistant's sandbox
passes the same 35 checks as outside it (Home Assistant and every device, Telegram, the backup's bucket, the routers);
the web UI serves its pages inside its own; Chromium launches inside Telegram's, and reaches the screen; and the screen
makes its socket inside its own. A system call outside the filter fails with an error rather than killing the service.
The web UI was checked with no devices (3.1); its 3.4 now is worked out, not measured: its unit differs from the voice
assistant's only in hiding Docker, which `systemd-analyze` doesn't score.

Still open:

- **The service manager.** The voice assistant, Telegram and the web UI can reach the user's systemd over D-Bus
  (`/run/user/<uid>/bus`), and anything that can ask it to start a unit (`systemd-run --user`) runs that unsandboxed.
  They need it: a changed setting restarts TARS through `systemd-run` (settings.py), a maintainer's tap on Install
  starts the updater (Telegram), and the Status page asks `systemctl` each unit's state (the web UI). Closing it needs
  those to go through something narrower than the whole manager.
- **Docker.** The voice assistant, Telegram and the backup can reach Docker's socket, which is root (below): Home
  Assistant's setup history runs `git` in Home Assistant's container (haconfig.py), from the Home Assistant tool and
  from each backup pass, and a restore stops and starts that container.
- **The checkout.** Each sandboxed service can write the whole project folder, and the updater, unsandboxed, runs
  `deploy/install-pi.sh`, `uv` and `git` from it: a service that rewrote one of those would have it run outside its
  sandbox at the next update. Narrowing it means each service writes only what it keeps (`voice_data/`,
  `config.local.toml`, `models/personal/`, and what a plugin keeps), and `uv run` no longer syncs `.venv` as a service
  starts; each service has to be run on the Pi inside the narrower sandbox before it's changed.
- A user service can't filter addresses (`IPAddressDeny` needs a system service), so the web UI can still reach the
  internet (it reads recordings from the bucket, too); and every service can read `.env`, the vault and
  `voice_data/`, since they all run as one user. A web UI as its own user, which can't, needs the services moved to
  the system manager.

**Root.** On the Pi, that user is in the `docker` group, so Home Assistant can run in Docker beside TARS; and a member
of the `docker` group can start a container with the whole disk in it, which makes it effectively root. That's
accepted, and written down here because it's easy to miss: anything that runs as TARS's user could take the machine.
Besides that, the one thing TARS runs as root is `deploy/tars-system` (installed root-owned as
`/usr/local/sbin/tars-system`, with a sudoers rule for it alone, with no arguments): the packages
`deploy/pi-packages.txt` lists, from the system's own signed repositories (names checked); TARS's two device rules,
refused if one would run a program as root; and lingering. A change to it is installed only by a person running
`deploy/install-pi.sh`, never by an update ([updates](deployment.md#updates)), so TARS can't rewrite its own root
script.
