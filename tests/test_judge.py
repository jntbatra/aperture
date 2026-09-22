"""Tests for the answer-quality judge.

The failure behind this: asked which kitchens cancel the most orders, the agent
returned a correct, faithful, well-written list of the kitchens with the most
orders. Every figure traced to a row, the query ran, and the answer was about
the wrong thing. Execution accuracy cannot see it — the SQL ran. The
faithfulness guard cannot see it — the figures are real.
"""

from __future__ import annotations

from sqlagent.judge import Verdict, judge_answer, summarise_verdicts
from sqlagent.llm.mantle import Completion


class StubClient:
    def __init__(self, reply: str = "", *, fail: bool = False):
        self._reply = reply
        self._fail = fail
        self.prompts: list[str] = []
        self.models: list[str | None] = []

    def complete(self, prompt, *, model=None, system=None, temperature=None):
        self.prompts.append(prompt)
        self.models.append(model)
        if self._fail:
            raise RuntimeError("model unavailable")
        return Completion(
            text=self._reply, input_tokens=5, output_tokens=5, model="m", seconds=0.01
        )


CLEAN = '{"answers_question": true, "complete": true, "supported": true, "problem": ""}'


def grade(reply: str = CLEAN, *, fail: bool = False, client=None) -> Verdict | None:
    return judge_answer(
        client or StubClient(reply, fail=fail),
        question="which kitchens cancel the most orders?",
        sql="SELECT kitchen, count(*) FROM orders GROUP BY kitchen",
        result_preview="kitchen | count\nA | 12",
        answer="Kitchen A has the most, with 12.",
        model="judge-model",
    )


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


def test_a_clean_answer_passes():
    verdict = grade()

    assert verdict is not None
    assert verdict.ok
    assert verdict.defects == ()


def test_an_answer_to_a_different_question_is_caught():
    verdict = grade(
        '{"answers_question": false, "complete": true, "supported": true, '
        '"problem": "reports the most orders, not the most cancellations"}'
    )

    assert verdict is not None
    assert not verdict.ok
    assert verdict.defects == ("answers_question",)
    assert "cancellations" in verdict.problem


def test_a_dropped_sub_question_is_caught():
    verdict = grade(
        '{"answers_question": true, "complete": false, "supported": true, '
        '"problem": "asked by month and by category; only by month is answered"}'
    )

    assert verdict is not None
    assert verdict.defects == ("complete",)


def test_an_unsupported_claim_is_caught():
    verdict = grade(
        '{"answers_question": true, "complete": true, "supported": false, '
        '"problem": "attributes the drop to seasonality, which no row shows"}'
    )

    assert verdict is not None
    assert verdict.defects == ("supported",)


def test_several_defects_are_all_reported():
    verdict = grade(
        '{"answers_question": false, "complete": false, "supported": true, "problem": "x"}'
    )

    assert verdict is not None
    assert verdict.defects == ("answers_question", "complete")


def test_the_judge_sees_the_question_the_query_the_rows_and_the_answer():
    """Missing any one of the four makes the judgement impossible rather than
    merely harder — the rows without the question cannot be graded at all."""
    client = StubClient(CLEAN)
    grade(client=client)

    prompt = client.prompts[0]
    for fragment in (
        "which kitchens cancel",
        "GROUP BY kitchen",
        "A | 12",
        "Kitchen A has the most",
    ):
        assert fragment in prompt


def test_the_judge_model_is_used():
    """Not the answering model. A reviewer and an author failing the same way
    is the failure mode of any self-review scheme, and here that failure
    produces a good score rather than a bad answer."""
    client = StubClient(CLEAN)
    grade(client=client)

    assert client.models == ["judge-model"]


# --------------------------------------------------------------------------
# Not passing what it did not grade
#
# A judge that reports "fine" when it could not be reached inflates exactly the
# number it exists to produce.
# --------------------------------------------------------------------------


def test_an_unreachable_judge_returns_no_verdict():
    assert grade(fail=True) is None


def test_unparseable_json_returns_no_verdict():
    assert grade("I think the answer is quite good, actually") is None


def test_a_missing_field_is_ungraded_not_passed():
    assert grade('{"answers_question": true, "complete": true}') is None


def test_a_non_boolean_field_is_ungraded_not_passed():
    """`"yes"` is truthy in Python, and would have scored as a pass."""
    assert grade(
        '{"answers_question": "yes", "complete": true, "supported": true}'
    ) is None


def test_a_json_array_is_ungraded():
    assert grade("[1, 2, 3]") is None


def test_a_missing_problem_is_just_empty():
    """Only the three verdicts are required. The sentence is commentary."""
    verdict = grade('{"answers_question": true, "complete": true, "supported": true}')

    assert verdict is not None
    assert verdict.problem == ""


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def good() -> Verdict:
    return Verdict(True, True, True)


def bad(**kwargs) -> Verdict:
    return Verdict(**{"answers_question": True, "complete": True, "supported": True, **kwargs})


def test_quality_is_the_share_of_graded_answers_with_no_defect():
    report = summarise_verdicts([good(), good(), bad(supported=False), good()])

    assert report.graded == 4
    assert report.quality == 0.75


def test_ungraded_answers_are_counted_separately_not_as_failures():
    """Folding them in would make a flaky judge look like a regression in the
    agent, which is the wrong thing to be alarmed by."""
    report = summarise_verdicts([good(), None, None])

    assert report.graded == 1
    assert report.ungraded == 2
    assert report.quality == 1.0


def test_defects_are_counted_by_category():
    """A rate says the answers got worse. The breakdown says whether they
    started answering the wrong question or started overclaiming."""
    report = summarise_verdicts(
        [bad(answers_question=False), bad(answers_question=False), bad(complete=False)]
    )

    assert report.by_defect == {"answers_question": 2, "complete": 1}


def test_nothing_graded_is_not_a_division_by_zero():
    report = summarise_verdicts([None, None])

    assert report.graded == 0
    assert report.quality == 0.0


def test_the_report_serialises_for_the_results_file():
    payload = summarise_verdicts([good(), bad(complete=False)]).to_dict()

    assert payload["quality"] == 0.5
    assert payload["by_defect"] == {"complete": 1}


# --------------------------------------------------------------------------
# Round-tripping through a results file
#
# A run is stored as outcomes, not verdicts, so the report is re-derived from
# the defect names. A name misspelled in one of the two places produces a
# silently better score rather than an error.
# --------------------------------------------------------------------------


def test_a_verdict_survives_the_round_trip():
    for verdict in (
        Verdict(True, True, True),
        Verdict(False, True, True),
        Verdict(True, False, True),
        Verdict(True, True, False),
        Verdict(False, False, False),
    ):
        assert Verdict.from_defects(verdict.defects) == verdict


def test_an_unknown_defect_name_is_an_error_not_a_pass():
    """Silently ignoring it would score a graded failure as clean."""
    import pytest

    with pytest.raises(ValueError, match="unknown defect"):
        Verdict.from_defects(["answers_qeustion"])


def test_the_problem_sentence_survives_the_round_trip():
    rebuilt = Verdict.from_defects(["complete"], "only the monthly breakdown")

    assert rebuilt.problem == "only the monthly breakdown"
