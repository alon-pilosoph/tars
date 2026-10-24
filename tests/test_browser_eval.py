"""The browser's eval tool (tools/browser_eval.py): what a tidying run is asked after a task that fills a basket."""

import importlib.util
from pathlib import Path

TOOL = Path(__file__).parents[1] / "tools/browser_eval.py"


def eval_tool():
    spec = importlib.util.spec_from_file_location("browser_eval", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_tidying_run_is_told_the_site_and_what_the_run_said():
    turns = [{"call": "act", "url": "https://wolt.com/x", "said": "ok"}, {"call": "done", "url": "", "said": "ok"}]
    goal = eval_tool().tidy_goal(turns, "Your margherita from Pizza Har Sinai is at checkout, ₪66.")
    assert "Empty the basket or cart on wolt.com" in goal and "Pizza Har Sinai" in goal
