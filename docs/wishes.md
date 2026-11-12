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

1. **The wish** (`tars` tool, `wish`) is kept in the `wishes` table. With `[wishes]` on, the maintainers get it straight
   through Telegram's API with a Build it button (`wishes.Wishes.offer`), and the issue text it would post.
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
- **Nothing merges itself.** `main` must be protected so that a reviewed pull request is the only way in (on TARS's
  own repository: pull requests with one approval, which only the owner may bypass); the workflow's token can push
  branches, so without that it could push to `main`.
- **A build that stops moving stops**: after `[wishes] tries` fixes, with no pull request after three hours, or with
  CI never finishing on its latest commit, or no fix pushed after it was sent back, for three hours. A pull request
  that's built and waiting for review waits as long as it takes.

## Setting it up

1. **A GitHub token for TARS**: a fine-grained personal access token for the repository only, with Issues read and
   write (it opens the issue, and comments on the pull request), Pull requests read, Checks read (CI on a commit) and
   Metadata read; nothing else. Put it in `.env` as `GITHUB_TOKEN`. It's the owner's: the workflow only answers the
   owner.
2. **Claude Code on the repository**: install the Claude GitHub App on it (`/install-github-app` in Claude Code does
   both), and add an Anthropic API key as the repository secret `ANTHROPIC_API_KEY`. Pushes the app makes run CI, so
   TARS has something to follow.
3. **Protect `main`**: pull requests only, with an approval (and CI) required. Without it, the workflow's token could
   push to `main`.
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
