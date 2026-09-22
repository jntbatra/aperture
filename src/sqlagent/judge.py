"""Grade an answer on the things no mechanical check can see.

What is already measured, and what is not
-----------------------------------------
Execution accuracy compares result rows against a gold query. It measures the
**SQL** and says nothing about the sentence written from it. The faithfulness
guard (:mod:`sqlagent.guards.faithfulness`) checks that every figure in the
answer appears in the rows — a real check, and a narrow one: an answer can
quote every number correctly and still be an answer to a different question.

Observed, and the reason this exists:

    Q: which kitchens cancel the most orders?
    A: <a correct, faithful, well-written list of the kitchens with the most
       orders>

Every figure traced to a row. The query ran. The answer was about the wrong
thing, and nothing in the system noticed.

Three verdicts, not a score
---------------------------
A 1–5 rating is uncalibrated between runs and unactionable within one — "3.4"
does not tell anyone what to fix. Three named defects do:

* **answers_question** — is this an answer to what was asked, or to a
  neighbouring question?
* **complete** — was every part of the question addressed? "Revenue by month
  and by category" answered only by month is a common, quiet failure.
* **supported** — does the answer claim only what the rows show? Causes,
  trends and comparisons the query never computed are the usual overreach.

Where this runs
---------------
Offline, over a finished run. Deliberately not in the request path: a judge
that grades an answer after it has been written has nothing to do with the
result but delay it, and the useful version of that idea is the critic, which
reviews the SQL *before* it runs. This measures — it is how "is the critic
worth its model call" becomes a number rather than an opinion.

Why a failed judgement is not a pass
------------------------------------
A judge that returns "fine" when it could not be reached inflates exactly the
number it exists to produce. Failure returns ``None``, and the caller reports
how many questions went ungraded alongside the score.

Why the judge should not be the answering model
-----------------------------------------------
A reviewer and an author failing the same way is the failure mode of any
self-review scheme. ``judge_model`` is separate from both the light and strong
tiers for that reason. It is a partial defence, not a solution: two models from
the same family share more failure modes than either shares with a human.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlagent.llm.mantle import json_from_reply

logger = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = """\
You grade one answer written from the result of one SQL query.

You are given the question, the query, the rows it returned, and the answer.
Judge only what is in front of you. You cannot run anything, and you must not
speculate about data you were not shown.

Reply with JSON only:
  {"answers_question": true, "complete": true, "supported": true, "problem": ""}

answers_question - false if the answer is about something other than what was
  asked. The most common form: the question asked about one thing (cancelled
  orders, returning customers, items never sold) and the answer is a correct
  statement about a neighbouring thing (all orders, all customers, items sold
  least). The figures being right does not make this true.

complete - false if part of the question went unaddressed. A question asking
  for two breakdowns, or for a figure "and why", is not complete with one of
  them. An answer that says plainly it could not do the rest is complete; one
  that silently drops it is not.

supported - false if the answer states something the rows do not show. Causes,
  trends over a period the query did not cover, comparisons against figures
  that are not there. Rounding and ordinary phrasing are fine.

problem - one sentence naming the single worst defect, in plain words. Empty
  string when all three are true. Do not restate the answer or grade the
  writing style.
"""


@dataclass(frozen=True, slots=True)
class Verdict:
    """One graded answer."""

    answers_question: bool
    complete: bool
    supported: bool
    problem: str = ""

    @property
    def ok(self) -> bool:
        """All three hold. The headline number is the rate of this."""
        return self.answers_question and self.complete and self.supported

    @classmethod
    def from_defects(cls, defects, problem: str = "") -> Verdict:
        """Rebuild a verdict from the names of what failed.

        A run is stored as outcomes, not as verdicts, so re-deriving the report
        from a results file goes through here. Round-tripping through
        :attr:`defects` is checked by a test, because a name misspelled in one
        of the two places produces a silently better score rather than an
        error.
        """
        failed = set(defects)
        unknown = failed - {"answers_question", "complete", "supported"}
        if unknown:
            raise ValueError(f"unknown defect(s): {sorted(unknown)}")
        return cls(
            answers_question="answers_question" not in failed,
            complete="complete" not in failed,
            supported="supported" not in failed,
            problem=problem,
        )

    @property
    def defects(self) -> tuple[str, ...]:
        """Which criteria failed, for counting by category.

        A rate alone says the answers got worse; the breakdown says whether
        they started answering the wrong question or started overclaiming,
        which are different problems with different fixes.
        """
        failed = []
        if not self.answers_question:
            failed.append("answers_question")
        if not self.complete:
            failed.append("complete")
        if not self.supported:
            failed.append("supported")
        return tuple(failed)


def judge_answer(
    client,
    *,
    question: str,
    sql: str,
    result_preview: str,
    answer: str,
    model: str,
    trace=None,
) -> Verdict | None:
    """Grade one answer, or return ``None`` if it could not be graded.

    Args:
        result_preview: The rows as text, already truncated. The judge sees the
            same bounded preview the answer was written from — grading against
            the full result would penalise an answer for not mentioning rows
            nobody showed it.

    Returns:
        A verdict, or ``None`` on any failure. Never raises, and never returns
        a passing verdict it did not actually make: an unreachable judge that
        reports "fine" inflates the one number this exists to produce.
    """
    prompt = (
        f"Question: {question}\n\n"
        f"Query:\n{sql}\n\n"
        f"Rows returned:\n{result_preview}\n\n"
        f"Answer given:\n{answer}\n\n"
        "Grade it. JSON only."
    )

    try:
        completion = client.complete(prompt, model=model, system=JUDGE_SYSTEM_PROMPT)
        if trace is not None:
            trace.record(completion)
        payload = json_from_reply(completion.text)
    except Exception as exc:  # noqa: BLE001 - measurement, never fatal
        logger.warning("could not judge answer: %s", exc)
        return None

    if not isinstance(payload, dict):
        logger.warning("judge returned %s, not an object", type(payload).__name__)
        return None

    # Absent means ungraded, not passed. A judge that omits a field has not
    # made that judgement, and defaulting it to True would quietly turn a
    # malformed reply into a clean score.
    for field in ("answers_question", "complete", "supported"):
        if not isinstance(payload.get(field), bool):
            logger.warning("judge omitted or mistyped %r", field)
            return None

    return Verdict(
        answers_question=payload["answers_question"],
        complete=payload["complete"],
        supported=payload["supported"],
        problem=str(payload.get("problem") or "").strip(),
    )


@dataclass(frozen=True, slots=True)
class JudgeReport:
    """Verdicts over a run, reduced to the numbers worth quoting."""

    graded: int
    ungraded: int
    good: int
    by_defect: dict[str, int]

    @property
    def quality(self) -> float:
        """Share of *graded* answers with no defect.

        Over the graded ones, not the attempted ones. Counting an ungraded
        answer as a failure would make a flaky judge look like a regression in
        the agent, which is the wrong thing to be alarmed by — so ``ungraded``
        is reported beside this rather than folded into it.
        """
        return round(self.good / self.graded, 4) if self.graded else 0.0

    def to_dict(self) -> dict:
        return {
            "graded": self.graded,
            "ungraded": self.ungraded,
            "good": self.good,
            "quality": self.quality,
            "by_defect": dict(sorted(self.by_defect.items())),
        }


def summarise_verdicts(verdicts: list[Verdict | None]) -> JudgeReport:
    """Reduce per-question verdicts to a report."""
    graded = [v for v in verdicts if v is not None]
    by_defect: dict[str, int] = {}
    for verdict in graded:
        for defect in verdict.defects:
            by_defect[defect] = by_defect.get(defect, 0) + 1

    return JudgeReport(
        graded=len(graded),
        ungraded=len(verdicts) - len(graded),
        good=sum(1 for v in graded if v.ok),
        by_defect=by_defect,
    )
