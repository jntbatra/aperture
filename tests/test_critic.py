"""Tests for Loop C — the critic, and for voting.

Both are toggles. The property these protect is that switching one on cannot
introduce a *new* way to fail: an advisory check that can break a request is
worse than no check, because the query it is reviewing was already safe.
"""

from __future__ import annotations

from sqlagent.guards.critic import Critique, build_critic_prompt, review
from sqlagent.llm.mantle import Completion
from sqlagent.voting import canonical, tally


class StubClient:
    """Returns canned replies, or raises."""

    def __init__(self, *replies: str, fail: bool = False):
        self._replies = list(replies)
        self._fail = fail
        self.calls = 0
        self.temperatures: list[float | None] = []

    def complete(self, prompt, *, model=None, system=None, temperature=None):
        self.calls += 1
        self.temperatures.append(temperature)
        if self._fail:
            raise RuntimeError("model unavailable")
        text = self._replies[min(self.calls - 1, len(self._replies) - 1)]
        return Completion(
            text=text, input_tokens=10, output_tokens=5, model=model or "m", seconds=0.01
        )


# --------------------------------------------------------------------------
# The critic
# --------------------------------------------------------------------------


def test_an_approved_query_passes():
    critique = review(
        StubClient('{"ok": true}'),
        question="how many orders?",
        schema_text="Tables:\n  orders(id INT)",
        sql="SELECT count(*) FROM orders",
        model="m",
    )

    assert critique.ok
    assert bool(critique) is True


def test_a_rejection_carries_the_specific_defect():
    """"This is wrong, try again" reliably produces a differently-wrong query.
    The named defect is what makes the repair actionable."""
    critique = review(
        StubClient(
            '{"ok": false, "problem": "it counts order_items rows, but the '
            'question asks how many orders"}'
        ),
        question="how many orders?",
        schema_text="Tables:\n  orders(id INT)",
        sql="SELECT count(*) FROM order_items",
        model="m",
    )

    assert not critique.ok
    assert "order_items rows" in critique.problem


def test_a_rejection_with_no_stated_problem_is_treated_as_approval():
    """It would enter a repair prompt as "this is wrong" and produce noise."""
    critique = review(
        StubClient('{"ok": false}'),
        question="q",
        schema_text="",
        sql="SELECT 1",
        model="m",
    )

    assert critique.ok


def test_an_empty_problem_string_is_treated_as_approval():
    critique = review(
        StubClient('{"ok": false, "problem": "   "}'),
        question="q",
        schema_text="",
        sql="SELECT 1",
        model="m",
    )

    assert critique.ok


def test_a_model_failure_approves_rather_than_failing_the_request():
    """The query under review is already validated and safe. A throttled call
    must not become a new way to fail."""
    critique = review(
        StubClient(fail=True),
        question="q",
        schema_text="",
        sql="SELECT 1",
        model="m",
    )

    assert critique.ok


def test_a_malformed_reply_approves():
    critique = review(
        StubClient("I think the query looks fine, honestly"),
        question="q",
        schema_text="",
        sql="SELECT 1",
        model="m",
    )

    assert critique.ok


def test_the_critic_sees_the_schema():
    """Most real defects are invisible without it — a filter on the wrong
    column looks reasonable until you can see what that column holds."""
    prompt = build_critic_prompt(
        "how many orders?", "Tables:\n  orders(status TEXT)", "SELECT count(*) FROM orders"
    )

    assert "orders(status TEXT)" in prompt
    assert "how many orders?" in prompt
    assert "SELECT count(*) FROM orders" in prompt


def test_a_critique_is_falsy_when_it_rejects():
    assert not Critique(ok=False, problem="wrong grain")


# --------------------------------------------------------------------------
# Voting
# --------------------------------------------------------------------------


def test_the_majority_wins():
    vote = tally(
        [
            "SELECT count(*) FROM orders",
            "SELECT count(*) FROM orders",
            "SELECT count(*) FROM customers",
        ],
        dialect="postgres",
    )

    assert "orders" in vote.sql
    assert vote.agreement == 2
    assert vote.total == 3


def test_formatting_differences_do_not_split_the_vote():
    """`SELECT a, b FROM t` and `select a,b from t` are the same query."""
    vote = tally(
        [
            "SELECT a, b FROM t",
            "select a,b from t",
            "SELECT z FROM t",
        ],
        dialect="postgres",
    )

    assert vote.agreement == 2


def test_the_winner_is_returned_as_written_not_reformatted():
    """Executing a re-rendered query means running something the model never
    produced, which makes a failure harder to trace back."""
    vote = tally(["select a from t", "select a from t"], dialect="postgres")

    assert vote.sql == "select a from t"


def test_unanimity_is_reported():
    vote = tally(["SELECT 1", "SELECT 1"], dialect="postgres")

    assert vote.unanimous
    assert vote.confidence == 1.0


def test_a_scattered_vote_reports_low_confidence():
    """The useful signal: the model was guessing."""
    vote = tally(["SELECT 1", "SELECT 2", "SELECT 3"], dialect="postgres")

    assert vote.agreement == 1
    assert vote.confidence < 0.5


def test_a_tie_is_broken_by_order_of_generation():
    """Deterministic, so the same inputs produce the same output."""
    vote = tally(["SELECT a FROM t", "SELECT b FROM t"], dialect="postgres")

    assert vote.sql == "SELECT a FROM t"


def test_an_unparseable_candidate_can_still_lose():
    vote = tally(
        ["not sql at all", "SELECT 1 FROM t", "SELECT 1 FROM t"], dialect="postgres"
    )

    assert vote.agreement == 2


def test_blank_candidates_are_ignored():
    vote = tally(["", "   ", "SELECT 1"], dialect="postgres")

    assert vote.total == 1


def test_no_usable_candidates_yields_nothing():
    assert tally(["", "  "], dialect="postgres") is None


def test_canonicalising_unparseable_sql_does_not_raise():
    assert canonical("}{ nonsense", dialect="postgres")
