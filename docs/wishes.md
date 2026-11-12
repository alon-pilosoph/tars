# Wishes that come true

Stacey asks TARS for something it can't do yet. Some days later, unprompted: "Remember you asked me for this: read the
kindergarten's WhatsApp updates? I can do it now." In between, the only person who did anything reviewed a pull
request.

| What | What happens |
|---|---|
| Someone asks for something TARS really can't do | it's noted as a wish, with what stopped it ([knowing itself](architecture.md)) |
| The maintainers' chats | "Stacey asked me: … I couldn't: …. Want me to have it built?", with exactly what would be posted to GitHub, and a **Build it** button |
| A maintainer taps Build it | an issue opens on GitHub, labelled `wish`; Claude Code builds it and opens a pull request |
| CI passes | the maintainers hear what it does, with the link |
| CI fails | Claude Code is sent back to fix it, three times at most, then the maintainers hear it was given up on |
| It's stuck, or closed unmerged | the maintainers hear it was dropped |
| It's merged | the updater installs it ([updates](deployment.md#updates)) |
| It's installed | whoever asked is told, once: in their chat if they asked in one, otherwise the next time they speak to TARS |

## How it works

1. **The wish** (`tars` tool, `wish`) is kept in the `wishes` table, once the turn that notes it is over (a reply
   thrown away notes none), and never by a reply that searched the web, whose pages could word it, nor by a turn a
   door's lock set off. With `[wishes]` on, the maintainers get it straight
   through Telegram's API with a Build it button (`wishes.Wishes.offer`), and the issue it would post, whole: title and
   body, exactly as they'd be posted. What was asked for is cut at 300 characters and what stopped TARS at 1,200
   (`ASKED_MAX`, `WHY_MAX`), with an ellipsis, so the issue is at most about 1,700 characters and the offer always fits
   one Telegram message (4,096); if anything is cut to fit, it's the line above the issue, never the issue.
2. **A maintainer's tap** (`wishes.tap_listener`, in the Telegram service) opens the issue on `[wishes] repo` with the
   GitHub token in `.env`. Only a tap from a maintainer's own chat counts; anyone else's is acknowledged and ignored.
3. **The workflow** (`.github/workflows/wish.yml`) hands the issue to
   [Claude Code's GitHub Action](https://github.com/anthropics/claude-code-action). It builds the wish on a branch,
   keeping to [`.github/wish.md`](../.github/wish.md) (the repository's rules: tests, docs with the code, nothing
   household-specific outside `examples/`, every paid service optional, the rules held in code left alone), and opens
   one pull request that closes the issue.
4. **TARS follows it** (`Wishes.follow`, every five minutes in the Telegram service): it finds Claude Code's pull
   request for the issue, reads CI on its latest commit, and says what happened. Only a pull request opened by the
   Claude app (`claude[bot]`), from this repository's own `claude/issue-<number>-` branch, counts: anyone can open a
   pull request on a public repository, and one that says it closes the issue is no proof. A failing commit gets one
   `@claude` comment saying which checks failed; the workflow's second job checks the pull request is Claude Code's
   own, then has it push a fix to that branch.
5. **Merged is installed** by the updater, at a quiet moment, checked, and taken back if it fails.
6. **Granted.** Once the checkout TARS runs from has the merge commit (`git merge-base --is-ancestor`), the wish is
   marked granted and the person who asked is told. A wish nobody's name was on is told to the maintainers. This
   needs `[updates]` on: without it, nothing installs the merge, and the wish waits until the checkout is updated.

## What's held in code

- **Nothing is built without a maintainer's tap**, and at most `[wishes] monthly` builds start in 30 days. TARS can't
  see what a run costs, so the cap is a count, not dollars.
- **What a maintainer approves is all of what's posted.** The issue's text is capped so the offer shows it whole, and
  the tap posts that same text; no part of it goes to GitHub unseen.
- **The coding agent has the repository, and no more of the house than the issue.** It runs on GitHub's machines: no
  `.env`, no `config.local.toml`, no database. Its reach there is real, though: the Anthropic key, and the Claude
  app's token, which may push branches, open pull requests and comment on issues. The workflow scrubs both from the
  commands it runs as far as the action manages (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`), lets it run only the commands
  building needs (tests, lint, `git commit`, a push to its own `claude/issue-` branch), and shows it only the owner's
  and Claude's own comments.
- **Who asked isn't posted.** The issue has what was asked for and what stopped TARS, never who asked or the chat it
  came from. The model is told to write both without anyone's name or the house's own names for its rooms and
  devices; that's asked of it, not checked, so the maintainer sees the exact text before tapping.
- **Only the repository's owner sets the workflow off**, by labelling an issue or commenting `@claude` on a pull
  request: on a public repository, anyone else's issue or comment could otherwise spend the key.
- **What builds a wish is fenced off from it.** The issue's text came from a conversation, so it's outside-influenced,
  and it ends up instructing an agent that can push to the repository. `.github/wish.md` tells the agent never to
  change `.github/` (the workflows, CI, those instructions), `deploy/` (units, the installer, the root helper), how
  TARS updates and builds itself, how secrets are kept, or the safety guards (unlocking, drafts, orders), and to stop
  and say so when a wish would need it. CI holds the plainest of these in code: its `wish fence` check fails a pull
  request from a `claude/issue-` branch that changes anything under `.github/` or `deploy/`, or `updater.py`,
  `wishes.py`, `draft.py`, `homenet.py`, `browser/gate.py`, `browser/money.py` or `browser/vault.py`. The check can't
  be edited away from inside the pull request: GitHub refuses a push that changes a workflow from the Claude app
  without the Workflows permission, or from a workflow's own token.
- **Nothing merges itself**, once `main` is protected (step 3 of [setting it up](#setting-it-up), **required**): then
  a reviewed pull request, with CI and the `wish fence` check passing, is the only way in. Without it, the workflow's
  tokens, which can push branches, could push to `main`. TARS doesn't check that it's set up.
- **A build that stops moving stops**: after `[wishes] tries` fixes, with no pull request after three hours, or with
  CI never finishing on its latest commit, or no fix pushed after it was sent back, for three hours. A pull request
  that's built and waiting for review waits as long as it takes.

## Setting it up

1. **A GitHub token for TARS**: a fine-grained personal access token, the owner's (the workflow only answers the
   owner), with repository access to this repository only, and these permissions, nothing else:

   | Permission | Access | For |
   |---|---|---|
   | Issues | Read and write | opening the labelled issue (`POST /repos/…/issues`), and the `@claude` comment on a pull request (`POST /repos/…/issues/N/comments`) |
   | Pull requests | Read-only | finding and reading Claude Code's pull request (`GET /repos/…/pulls`, `/pulls/N`) |
   | Metadata | Read-only | mandatory: GitHub adds it to every fine-grained token |

   CI on a pull request's latest commit (`GET /repos/…/commits/SHA/check-runs`) has no fine-grained permission at all:
   GitHub answers it for a public repository whatever the token may do, which is why the repository must be public
   (see [limitations](#limitations)).
   Put it in `.env` as `GITHUB_TOKEN`. TARS reads it the first time it needs GitHub and keeps the one connection: a new
   token takes effect when the Telegram service restarts.
2. **Claude Code on the repository**: install the Claude GitHub App on it (`/install-github-app` in Claude Code does
   both), and add an Anthropic API key as the repository secret `ANTHROPIC_API_KEY`. Pushes the app makes run CI, so
   TARS has something to follow. Don't give the app's installation the Workflows permission: without it, GitHub
   refuses the app's pushes that change `.github/workflows/`, which is what keeps the `wish fence` check in place.
3. **Required: protect `main` with a branch ruleset.** Nothing else stops a push to `main`, and TARS doesn't check it.
   In the repository's Settings, Rules, Rulesets, a new branch ruleset:
   - Enforcement status **Active**, targeting the default branch.
   - **Restrict deletions** and **Block force pushes**.
   - **Require a pull request before merging**, with **1** required approval.
   - **Require status checks to pass**: `python`, `web` and `wish fence` (CI's jobs; `wish fence` is skipped, and
     passes, on every pull request that isn't a wish's).
   - Bypass list: only the **Repository admin** role, so the owner can still merge their own pull requests; never the
     Claude app or GitHub Actions.
4. **Turn on `[updates]`**, so a merge is installed and the wish granted.
5. **Turn it on** in `config.local.toml`, with `[household] maintainers` on Telegram:

   ```toml
   [wishes]
   enabled = true
   repo = "alon-pilosoph/tars"
   ```

   Then restart the Telegram service.

## Limitations

- **Not yet run end to end.** The pieces are tested (the issue, following the pull request through CI, fixing,
  giving up, granting, the taps), against a stand-in for GitHub's API; a real wish hasn't gone through the workflow
  yet.
- **Big wishes may not fit.** One that needs an account, hardware or a household decision can't be built by an agent
  alone; `.github/wish.md` asks it to open the pull request saying what's needed instead of building half of it.
- **The maintainer hears what the pull request says it does**, its first paragraph: a review is still the review.
- **The repository must be public.** Reading CI on a commit (check runs) isn't something a fine-grained token can be
  given; GitHub shows it for a public repository to anyone. On a private one, every pass would fail to read CI
  ("couldn't follow wish", in the Telegram service's log) and the wish would wait as building; only a classic token
  with the whole `repo` scope could read it, which is far more than TARS should hold.
- **Only some of the fence is code.** The `wish fence` check covers whole files; unlocking (in `home.py`), the Order
  tap (in `browser/errands.py`) and the web-search rule (`llm.py`, `home.py`) live in files a wish may well need to
  change, so there it's `.github/wish.md`, the tests that pin each guard ([security](security.md)) and the review that
  hold them.
