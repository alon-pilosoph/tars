"""evals.comment: a pull request comment's request, and the reply to it."""

import json

import pytest

from evals import comment
from evals.comment import Request, parse, result_reply


@pytest.mark.parametrize(
    "body, want",
    [
        ("#evals", Request()),
        ("#evals runs=5 tag=hand-off", Request(runs=5, tags=["hand-off"])),
        ("#EVALS scenario=cancel-that,got-it budget=3.5", Request(scenarios=["cancel-that", "got-it"], budget=3.5)),
        ("#evals estimate\nanything on later lines is ignored", Request(estimate_only=True)),
    ],
)
def test_a_comment_says_what_to_run(body, want):
    assert parse(body) == want


@pytest.mark.parametrize(
    "body, says",
    [
        ("#evals runs=50", "runs=50"),  # over the most runs
        ("#evals budget=20", "budget=20"),  # over the ceiling
        ("#evals tag=nonsense", "No scenario has the tag nonsense"),
        ("#evals scenario=nope", "No scenario nope"),
        ("#evals --fast", "--fast"),  # not a setting: the standard tier only
        ("please run evals", "starts with #evals"),
    ],
)
def test_a_comment_that_cant_be_run_says_why(body, says):
    with pytest.raises(ValueError, match=says):
        parse(body)
    assert "`#evals` runs the scenario evals" in comment.reply(body)  # and the help comes with it


def test_a_request_always_runs_with_a_budget_and_never_on_the_fast_tier():
    args = Request(runs=2, tags=["memory"], budget=1.5).args()
    assert args[:4] == ["--runs", "2", "--budget", "1.5"] and ["--tag", "memory"] == args[6:8]
    assert "--fast" not in args and "--no-regressions" not in args and "--fail-under" not in args


def test_the_reply_lists_what_wasnt_right_compares_with_the_baseline_and_says_what_it_cost(tmp_path, monkeypatch):
    baseline = tmp_path / "results.jsonl"
    was = {"date": "2026-10-17T15:56", "right": [10, 10]}
    was["scenarios"] = {
        "a": {"right": 5, "runs": 5, "fingerprint": "x"},
        "b": {"right": 5, "runs": 5, "fingerprint": "y"},
    }
    baseline.write_text(json.dumps(was) + "\n")
    monkeypatch.setattr(comment, "BASELINE", baseline)
    summary = {"right": [6, 10], "interval": [0.31, 0.83], "turns": [6, 10], "cost_usd": 0.4, "estimated_usd": 0.5}
    summary["scenarios"] = {
        "a": {"right": 1, "runs": 5, "fingerprint": "x"},
        "b": {"right": 5, "runs": 5, "fingerprint": "y"},
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    turn = {"route": "quick", "said": "It's twelve.", "wrong": ["doesn't say /Canberra/"]}
    (tmp_path / "runs.jsonl").write_text(json.dumps({"scenario": "a", "run": 2, "turns": [turn]}) + "\n")
    text = result_reply(tmp_path, Request(runs=5), status=0, run_url="https://github.com/x/y/actions/runs/1")
    assert text.startswith("### Evals: 6/10 runs right")
    assert "cost $0.40 (estimated $0.50, budget $2)" in text
    assert "| `a` | 1/5 |" in text and "`b`" not in text.split("Against")[0]
    assert "- worse: `a` 5/5 → 1/5" in text
    assert "`a` run 2, turn 1 (quick): doesn't say /Canberra/" in text and "> It's twelve." in text
    assert "[this run](https://github.com/x/y/actions/runs/1)" in text
