"""Tests for the benchmark harness's reporting.

Why these exist: the harness is the thing that produces every number quoted
about this project, and until now nothing tested it. A mistake here does not
crash — it prints a different score.

The run itself is not exercised (it needs the BIRD corpus and a model). The
reporting is, because that is where a judged run turns into a figure.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bird():
    """Load ``benchmarks/bird.py``, which is a script rather than a package."""
    spec = importlib.util.spec_from_file_location("bird", ROOT / "benchmarks" / "bird.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bird"] = module
    spec.loader.exec_module(module)
    return module


def outcome(bird, **kwargs):
    base = dict(
        question_id=1,
        db_id="db",
        question="q",
        difficulty="simple",
        correct=True,
        predicted_sql="SELECT 1",
        gold_sql="SELECT 1",
        error=None,
        repairs=0,
        model_calls=2,
        input_tokens=10,
        output_tokens=5,
        seconds=1.0,
    )
    return bird.Outcome(**{**base, **kwargs})


def written(bird, outcomes, tmp_path) -> dict:
    out = tmp_path / "run.json"
    bird.report(outcomes, elapsed=1.0, config=bird.Settings(database_url="sqlite://"), out=out)
    return json.loads(out.read_text())


def test_an_ungraded_run_records_no_quality(bird, tmp_path):
    """A run without --judge must not report a quality of zero, which would
    read as every answer being bad."""
    payload = written(bird, [outcome(bird)], tmp_path)

    assert payload["answer_quality"] is None


def test_a_graded_run_records_the_quality(bird, tmp_path):
    payload = written(
        bird,
        [
            outcome(bird, judged=True, judge_ok=True),
            outcome(bird, judged=True, judge_ok=False, judge_defects=("complete",)),
        ],
        tmp_path,
    )

    assert payload["answer_quality"]["quality"] == 0.5
    assert payload["answer_quality"]["by_defect"] == {"complete": 1}


def test_ungraded_answers_are_counted_not_failed(bird, tmp_path):
    payload = written(
        bird,
        [outcome(bird, judged=True, judge_ok=True), outcome(bird, judged=False)],
        tmp_path,
    )

    assert payload["answer_quality"]["graded"] == 1
    assert payload["answer_quality"]["ungraded"] == 1
    assert payload["answer_quality"]["quality"] == 1.0


def test_quality_is_reported_beside_accuracy_not_folded_into_it(bird, tmp_path):
    """A query can return exactly the gold rows and be written up as an answer
    to a different question. One combined number hides which went wrong."""
    payload = written(
        bird,
        [outcome(bird, correct=True, judged=True, judge_ok=False,
                 judge_defects=("answers_question",))],
        tmp_path,
    )

    assert payload["accuracy"] == 1.0
    assert payload["answer_quality"]["quality"] == 0.0


def test_the_judge_model_is_recorded_with_the_score(bird, tmp_path):
    """A result file that does not say who graded it cannot be compared
    against another one."""
    payload = written(bird, [outcome(bird, judged=True, judge_ok=True)], tmp_path)

    assert "judge_model" in payload["config"]


def test_the_answer_is_stored_so_a_run_can_be_re_judged(bird, tmp_path):
    """Judging is cheap; answering is not. A run that recorded only the verdict
    would have to be re-run to grade it differently."""
    payload = written(bird, [outcome(bird, answer="Kitchen A, with 12.")], tmp_path)

    assert payload["outcomes"][0]["answer"] == "Kitchen A, with 12."
