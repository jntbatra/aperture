"""Tests for the ambiguity-stability harness.

The harness answers one question: asked the same thing five times, does the
clarifier reach the same verdict? That is a property of the system and needs no
labels, which is what makes it the number worth having. The accuracy figure
beside it is only as good as one person's judgement about their own database.

The model calls are not exercised here. The aggregation is, because that is
where repeated verdicts become a figure someone quotes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "ambiguity_harness", ROOT / "benchmarks" / "ambiguity.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ambiguity_harness"] = module
    spec.loader.exec_module(module)
    return module


def result(harness, question: str, verdicts: list[bool], expected: bool | None = None):
    return harness.QuestionResult(
        question=question, expected=expected, verdicts=verdicts, asks=[[] for _ in verdicts]
    )


# --------------------------------------------------------------------------
# Stability — the number with no ground truth in it
# --------------------------------------------------------------------------


def test_repeats_that_all_agree_are_stable(harness):
    assert result(harness, "q", [True, True, True]).stable


def test_one_disagreement_makes_a_question_unstable(harness):
    assert not result(harness, "q", [True, True, False]).stable


def test_stability_is_the_share_of_questions_that_agreed_with_themselves(harness):
    report = harness.summarise(
        [
            result(harness, "a", [True, True, True]),
            result(harness, "b", [False, False, False]),
            result(harness, "c", [True, False, True]),
            result(harness, "d", [True, True, False]),
        ]
    )

    assert report["stability"] == 0.5
    assert report["stable"] == 2
    assert report["unstable"] == 2


def test_stability_needs_no_labels(harness):
    """The whole reason it is the primary number."""
    report = harness.summarise([result(harness, "a", [True, True])])

    assert report["stability"] == 1.0
    assert report["labelled"] == 0


def test_no_runs_is_not_a_division_by_zero(harness):
    assert harness.summarise([])["stability"] == 0.0


def test_a_question_with_no_verdicts_is_excluded(harness):
    """A question whose every repeat errored has not been measured, and
    counting it as stable would make an outage look like consistency."""
    report = harness.summarise([result(harness, "a", []), result(harness, "b", [True, True])])

    assert report["questions"] == 1


# --------------------------------------------------------------------------
# Agreement with the labels
# --------------------------------------------------------------------------


def test_the_majority_verdict_is_compared_against_the_label(harness):
    report = harness.summarise(
        [
            result(harness, "vague", [True, True, False], expected=True),
            result(harness, "clear", [False, False, False], expected=False),
        ]
    )

    assert report["agreement"] == 1.0


def test_a_majority_the_other_way_is_a_disagreement(harness):
    report = harness.summarise(
        [result(harness, "vague", [False, False, True], expected=True)]
    )

    assert report["agreement"] == 0.0


def test_a_tie_is_not_a_majority(harness):
    """Two of four is not "mostly ambiguous"."""
    assert not result(harness, "q", [True, True, False, False]).majority


def test_the_two_failure_directions_are_reported_separately(harness):
    """Asking about a clear question and failing to ask about a vague one are
    different failures with different fixes. One rate hides which is
    happening."""
    report = harness.summarise(
        [
            result(harness, "vague", [False, False], expected=True),
            result(harness, "clear", [True, True], expected=False),
        ]
    )

    assert report["asks_when_vague"] == 0.0
    assert report["asks_when_clear"] == 1.0
    assert report["agreement"] == 0.0


def test_unlabelled_questions_do_not_affect_agreement(harness):
    report = harness.summarise(
        [
            result(harness, "labelled", [True, True], expected=True),
            result(harness, "unlabelled", [False, False]),
        ]
    )

    assert report["labelled"] == 1
    assert report["agreement"] == 1.0


# --------------------------------------------------------------------------
# Which unstable questions to look at
# --------------------------------------------------------------------------


def test_the_most_evenly_split_question_is_listed_first(harness):
    """A question ambiguous 4 times in 5 needs a prompt fix; one at 1 in 5 is
    closer to noise. The coin-flips are the ones to read."""
    report = harness.summarise(
        [
            result(harness, "lopsided", [True, True, True, True, False]),
            result(harness, "coin flip", [True, True, False, False, True]),
        ]
    )

    assert report["least_stable"][0]["question"] == "coin flip"


def test_stable_questions_are_not_listed(harness):
    report = harness.summarise([result(harness, "steady", [True, True, True])])

    assert report["least_stable"] == []


def test_the_repeat_count_is_recorded(harness):
    """A stability figure without its k is not a measurement — k=2 and k=10 are
    very different claims."""
    assert harness.summarise([result(harness, "a", [True] * 5)])["repeats"] == 5


# --------------------------------------------------------------------------
# The question set
# --------------------------------------------------------------------------


def labelled(items):
    return [item for item in items if not item.get("borderline")]


def test_the_labelled_question_set_is_balanced(harness):
    """A set that is mostly vague scores well by always asking, and one that is
    mostly clear scores well by never asking. Neither measures anything."""
    items = labelled(json.loads((ROOT / "benchmarks" / "ambiguity_questions.json").read_text()))

    vague = sum(1 for item in items if item["ambiguous"])
    assert len(items) >= 20
    assert 0.4 <= vague / len(items) <= 0.6


def test_every_non_borderline_question_is_labelled(harness):
    items = labelled(json.loads((ROOT / "benchmarks" / "ambiguity_questions.json").read_text()))

    for item in items:
        assert isinstance(item.get("ambiguous"), bool), item
        assert item["question"].strip()


def test_every_vague_question_says_why_it_is_vague(harness):
    """A label with no reason is an assertion. The reason is what someone
    disagreeing with the score has to argue against."""
    items = labelled(json.loads((ROOT / "benchmarks" / "ambiguity_questions.json").read_text()))

    for item in items:
        if item["ambiguous"]:
            assert item.get("why"), item["question"]


def test_borderline_questions_carry_no_label(harness):
    """Deliberately unlabelled. A question a careful person could argue either
    way has no ground truth to assert, and labelling it would be stating an
    answer rather than measuring one. They contribute to stability, which needs
    no labels and is the number that matters."""
    items = json.loads((ROOT / "benchmarks" / "ambiguity_questions.json").read_text())
    borderline = [item for item in items if item.get("borderline")]

    assert borderline
    for item in borderline:
        assert "ambiguous" not in item, item["question"]


def test_the_set_contains_questions_that_could_go_either_way(harness):
    """Without them, 100% agreement means the check can tell obvious from
    obvious. "How many orders were cancelled last month?" is where it might
    actually break — "last month" is either a clear relative period or an
    unbounded time word, and both readings are defensible."""
    items = json.loads((ROOT / "benchmarks" / "ambiguity_questions.json").read_text())

    assert sum(1 for item in items if item.get("borderline")) >= 10


def test_borderline_questions_still_reach_the_runner(harness):
    """`expected` is None for them, which the summary must treat as unlabelled
    rather than as False — counted as "not ambiguous" they would silently
    become a claim nobody made."""
    result = harness.QuestionResult(question="q", expected=None, verdicts=[True, True])
    report = harness.summarise([result])

    assert report["questions"] == 1
    assert report["labelled"] == 0
    assert report["stability"] == 1.0
