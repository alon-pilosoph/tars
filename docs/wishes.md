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
4. **TARS follows it** (`Wishes.follow`, every five minutes in the Telegram service): it finds the pull request that
   closes the issue, reads CI on its latest commit, and says what happened. A failing commit gets one `@claude` comment
   saying which checks failed; the workflow's second job takes that comment and pushes a fix.
5. **Merged is installed** by the updater, at a quiet moment, checked, and taken back if it fails.
6. **Granted.** Once the checkout TARS runs from has the merge commit (`git merge-base --is-ancestor`), the wish is
   marked granted and the person who asked is told. A wish nobody's name was on is told to the maintainers.

## What's held in code

- **Nothing is built without a maintainer's tap**, and at most `[wishes] monthly` builds start in 30 days. TARS can't
  see what a run costs, so the cap is a count, not dollars.
- **The coding agent has the repository and nothing else.** It runs on GitHub's machines with the workflow's
  permissions: no `.env`, no `config.local.toml`, no database. What it knows of the house is the issue.
- **What's posted names nobody.** The issue has what was asked for and what stopped TARS, never who asked or the
  chat it came from, and the model is told to write the wish without names. The maintainer sees the exact text before
  tapping.
- **Only the repository's owner sets the workflow off**, by labelling an issue or commenting `@claude` on a pull
  request: on a public repository, anyone else's issue or comment could otherwise spend the key.
- **Nothing merges itself.** A person reviews and merges; protect `main` so that's the only way in.
- **A build that keeps failing stops**: after `[wishes] tries` fixes, or with no pull request after three hours.

## Setting it up

1. **A GitHub token for TARS**: a fine-grained personal access token for the repository only, with Issues and Pull
   requests read and write. Put it in `.env` as `GITHUB_TOKEN`.
2. **Claude Code on the repository**: install the Claude GitHub App on it (`/install-github-app` in Claude Code does
   both), and add an Anthropic API key as the repository secret `ANTHROPIC_API_KEY`. Pushes the app makes run CI, so
   TARS has something to follow.
3. **Protect `main`**: pull requests only, with CI required.
4. **Turn it on** in `config.local.toml`, with `[household] maintainers` on Telegram:

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
