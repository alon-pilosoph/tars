# Building a wish

You're building something TARS, a household voice assistant, was asked for and couldn't do. The issue says what was
asked for and what stopped TARS. This repository is TARS; read `README.md` and `docs/architecture.md` first, then the
docs for the part you're changing.

What this repository keeps to, for every change:

- **One pull request, one clean commit**, titled the way the history is: what someone can now do, in plain words.
- **Tests** for what you add, in `tests/`, run with `uv run pytest -q`; `uv run ruff check src tests tools training
  evals examples` and `uv run ruff format` clean. Web UI changes: `npm run lint`, `npm run format:check` and
  `npm test` in `webui/`, and the built UI committed (`npm run build`).
- **Docs with the code**: the README, the docs in `docs/` for that part, and the site in `site/` where it describes
  it. Docs say what it does, how, and its limits; no claims that weren't measured.
- **Nothing about any particular household** in `src/`: a household's own devices and code go in `examples/`. Use the
  example household (Alon and Stacey) in tests and docs.
- **No secrets, keys or addresses** anywhere. A new service's key is read from `.env` by name, set in `config.toml`.
- **Every paid service optional**: TARS must start, and `--check` must pass, without it.
- **The rules held in code stay held**: unlocking needs a trusted voice saying "unlock"; nothing is ordered or paid
  without a tap; a reply that read the web changes nothing in the house. Don't weaken any of them.
- **A new tool or device** is described to the model in plain words, and what it can't do is said too, so TARS never
  claims more than it can.
- **No comments explaining code**, except a constraint that isn't obvious. Match the surrounding style.

If it can't be done well (it needs an account, hardware or a decision only the household can make), don't build half
of it: open the pull request with what you found and what's needed, and say so in its first line.
