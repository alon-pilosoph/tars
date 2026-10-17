"""Evaluations of TARS's brain: whole conversations (scenarios), hand-offs followed through, every turn checked.

    uv run python -m evals                     # every scenario, 3 runs each, Qwen on Cerebras, hand-offs to OpenAI
    uv run python -m evals --tag hand-off      # only those tagged hand-off
    uv run python -m evals --scenario weather-follow-up --runs 1 --show

See evals/README.md for what a scenario is and how a turn is checked.
"""
