"""Does the result answer the question? — the deterministic half and the model half.

Why this check exists at all is a measurement: over 150 BIRD questions, 61 of
the 62 failures were a valid query that returned the wrong rows. Nothing
raised, so there was nothing to repair against, and no earlier check could see
it — ``clarify`` and the critic never see a row, the faithfulness check never
sees the question.

These tests are in two halves because the check is. ``guards.evidence``
settles what arithmetic can settle, against the real database; ``intent`` puts
what is left, plus those facts, to a model. The first half needs no model at
all, which is the point of it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from sqlagent.guards.evidence import Evidence, gather
from sqlagent.intent import Verdict, build_intent_prompt, check_intent
from sqlagent.llm.mantle import Completion


class StubClient:
    """Returns canned replies, or raises."""

    def __init__(self, *replies: str, fail: bool = False):
        self._replies = list(replies)
        self._fail = fail
        self.prompts: list[str] = []

    def complete(self, prompt, *, model=None, system=None, temperature=None):
        self.prompts.append(prompt)
        if self._fail:
            raise RuntimeError("model unavailable")
        text_ = self._replies[min(len(self.prompts) - 1, len(self._replies) - 1)]
        return Completion(
            text=text_, input_tokens=10, output_tokens=5, model=model or "m", seconds=0.01
        )


# --------------------------------------------------------------------------
# The deterministic half
#
# Three failure shapes, all taken from real wrong answers rather than invented:
# a filter comparing against a value that occurs nowhere, a filter that
# excluded nothing, and a named thing from the question missing from the SQL
# entirely.
# --------------------------------------------------------------------------


@pytest.fixture
def connection():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE orders (id INTEGER, category TEXT, status TEXT, total INT)")
        )
        connection.execute(
            text(
                "INSERT INTO orders VALUES "
                "(1, 'Cravings Deals ⭐', 'DELIVERED', 400), "
                "(2, 'Cravings Deals ⭐', 'CANCELLED', 300), "
                "(3, 'Biryani', 'DELIVERED', 500)"
            )
        )
        yield connection


def test_a_filter_matching_nothing_is_a_finding(connection):
    """The real failure: the category is stored as 'Cravings Deals ⭐'.

    A query filtering on 'Cravings Deals' returns nothing, and "no rows" alone
    is indistinguishable from a correct answer of zero. Checking the column
    tells them apart, without a model.
    """
    found = gather(
        connection,
        question="how many orders in Cravings Deals?",
        sql="SELECT id FROM orders WHERE category = 'Cravings Deals'",
        dialect="sqlite",
        row_count=0,
    )

    assert found.unmatched_literals == (("orders.category", "Cravings Deals"),)
    assert found.worth_mentioning(0)
    assert "does not occur anywhere" in found.render(0)


def test_a_value_that_does_exist_is_not_a_finding(connection):
    found = gather(
        connection,
        question="orders in Biryani",
        sql="SELECT id FROM orders WHERE category = 'Biryani'",
        dialect="sqlite",
        row_count=1,
    )

    assert found.unmatched_literals == ()


def test_a_filter_that_excluded_nothing_is_a_finding(connection):
    """What a silently dropped constraint looks like from outside: the
    'filtered cohort' is the whole table."""
    found = gather(
        connection,
        question="orders worth more than nothing",
        sql="SELECT id FROM orders WHERE total > 0",
        dialect="sqlite",
        row_count=3,
    )

    assert found.unfiltered_count == 3
    assert found.worth_mentioning(3)
    assert "excluded nothing" in found.render(3)


def test_a_correct_count_is_not_reported_as_unfiltered(connection):
    """The regression this module nearly shipped with.

    Every un-grouped aggregate returns one row with or without its WHERE, so
    "same rows either way" was true of every correct COUNT ever written. It
    fired on this exact query, which is right. A check that teaches the model
    to distrust correct SQL is worse than no check.
    """
    found = gather(
        connection,
        question="how many delivered orders?",
        sql="SELECT count(*) FROM orders WHERE status = 'DELIVERED'",
        dialect="sqlite",
        row_count=1,
    )

    assert found.unfiltered_count is None
    assert not found.worth_mentioning(1)
    assert found.render(1) == ""


def test_a_named_thing_missing_from_the_sql_is_a_finding(connection):
    """The worst real failure: the category filter was dropped entirely, and
    the 'cohort' returned was the whole customer base."""
    found = gather(
        connection,
        question='retention for the "Cravings Deals" category',
        sql="SELECT count(*) FROM orders",
        dialect="sqlite",
        row_count=1,
    )

    assert found.dropped_terms == ("Cravings Deals",)
    assert "appears nowhere in the" in found.render(1)


def test_a_constraint_that_is_present_is_not_reported_as_dropped(connection):
    """Spelled differently still counts as present — the question is whether
    the constraint is there, not whether it matches character for character."""
    found = gather(
        connection,
        question='orders in "Cravings Deals"',
        sql="SELECT id FROM orders WHERE category LIKE '%Cravings%'",
        dialect="sqlite",
        row_count=2,
    )

    assert found.dropped_terms == ()


def test_unparseable_sql_still_reports_emptiness(connection):
    found = gather(
        connection,
        question="q",
        sql="this is not sql at all ((",
        dialect="sqlite",
        row_count=0,
    )

    assert found.empty


def test_nothing_to_say_renders_as_nothing():
    assert Evidence().render(5) == ""
    assert not Evidence().worth_mentioning(5)


# --------------------------------------------------------------------------
# The model half
# --------------------------------------------------------------------------


def _check(client, **overrides):
    kwargs = {
        "question": "how many customers ordered twice?",
        "sql": "SELECT count(*) FROM customers",
        "schema_text": "Tables:\n  customers(id INT)",
        "result_preview": "count\n-----\n760",
        "row_count": 1,
        "model": "m",
    }
    kwargs.update(overrides)
    return check_intent(client, **kwargs)


def test_a_result_that_answers_the_question_passes():
    verdict = _check(StubClient('{"verdict": "answers"}'))

    assert verdict.ok
    assert verdict.render() == ""


def test_a_mismatch_carries_the_defect_to_repair_against():
    verdict = _check(
        StubClient(
            '{"verdict": "mismatch", "reason": "it counts every customer, but '
            'the question asks only about customers who ordered twice"}'
        )
    )

    assert not verdict.ok
    assert "ordered twice" in verdict.reason
    assert verdict.render() == verdict.reason


def test_a_mismatch_with_no_reason_is_treated_as_acceptance():
    """It would enter the repair prompt as "this is wrong, try again", which
    reliably produces a differently-wrong query."""
    verdict = _check(StubClient('{"verdict": "mismatch", "reason": "  "}'))

    assert verdict.ok


def test_an_ask_carries_its_options():
    verdict = _check(
        StubClient(
            '{"verdict": "ask", "question": "twice in total or twice in a month?",'
            ' "options": ["in total", "in one month"]}'
        )
    )

    assert verdict.verdict == "ask"
    assert verdict.options == ("in total", "in one month")
    assert "in total / in one month" in verdict.render()


def test_asking_can_be_switched_off_and_downgrades_to_answers():
    """A harness has nobody to ask. Downgrading to *answers* rather than to
    *mismatch* is deliberate: a regeneration cannot be better informed about a
    question that was never put to anyone."""
    verdict = _check(
        StubClient('{"verdict": "ask", "question": "which sense of twice?"}'),
        allow_ask=False,
    )

    assert verdict.ok


def test_a_failed_check_accepts_the_result():
    """The last thing between a working result and the user. A check that
    cannot run must not be able to withhold an answer that was correctly
    produced."""
    assert _check(StubClient(fail=True)).ok


def test_an_unparseable_reply_accepts_the_result():
    assert _check(StubClient("I think it looks fine, honestly")).ok


def test_an_unknown_verdict_accepts_the_result():
    assert _check(StubClient('{"verdict": "probably not"}')).ok


def test_the_options_list_is_bounded():
    verdict = _check(
        StubClient(
            '{"verdict": "ask", "question": "which?", "options": '
            '["a", "b", "c", "d", "e", "f"]}'
        )
    )

    assert len(verdict.options) == 4


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------


def test_the_prompt_holds_the_question_the_sql_and_the_rows():
    """The whole reason this check exists. Every earlier one is missing one of
    the three, and the failures live in the comparison it cannot make."""
    prompt = build_intent_prompt(
        question="how many customers ordered twice?",
        conversation="",
        sql="SELECT count(*) FROM customers",
        schema_text="customers(id INT)",
        result_preview="count\n-----\n760",
        row_count=1,
        truncated=False,
    )

    assert "how many customers ordered twice?" in prompt
    assert "SELECT count(*) FROM customers" in prompt
    assert "760" in prompt


def test_established_facts_are_labelled_as_facts():
    """A model shown "this filter matches nothing" as settled behaves
    differently from one asked to notice it."""
    prompt = build_intent_prompt(
        question="q",
        conversation="",
        sql="SELECT 1",
        schema_text="",
        result_preview="(no rows)",
        row_count=0,
        truncated=False,
        evidence="- The query returned NO rows.",
    )

    assert "these are facts, not guesses" in prompt
    assert "returned NO rows" in prompt


def test_truncation_is_stated_rather_than_hidden():
    prompt = build_intent_prompt(
        question="q",
        conversation="",
        sql="SELECT 1",
        schema_text="",
        result_preview="...",
        row_count=1000,
        truncated=True,
    )

    assert "1000 rows (truncated)" in prompt


def test_a_verdict_is_immutable():
    with pytest.raises(AttributeError):
        Verdict("answers").verdict = "mismatch"  # type: ignore[misc]


def test_a_withheld_ask_is_recorded_not_just_logged():
    """The harness forbids asking, so a run reports zero asks. Without this,
    "0 asks" reads as a finding about the model when it is a setting."""
    verdict = _check(
        StubClient('{"verdict": "ask", "question": "which sense of twice?"}'),
        allow_ask=False,
    )

    assert verdict.ok
    assert verdict.withheld_question == "which sense of twice?"


def test_the_unfiltered_probe_refuses_a_join(connection):
    """Stripping the WHERE off a join is a cartesian product.

    This ran for 56 minutes at 189% CPU on two questions of a benchmark, and
    it explains a 3,760-second run that was blamed on API throttling. SQLite
    has no statement timeout in this codebase, so nothing stops it.
    """
    connection.execute(text("CREATE TABLE items (id INTEGER, order_id INTEGER)"))
    connection.execute(text("INSERT INTO items VALUES (1, 1), (2, 2), (3, 3)"))

    found = gather(
        connection,
        question="which orders have items?",
        sql=(
            "SELECT o.id FROM orders o JOIN items i ON i.order_id = o.id "
            "WHERE o.status = 'DELIVERED'"
        ),
        dialect="sqlite",
        row_count=2,
    )

    assert found.unfiltered_count is None


def test_the_unfiltered_probe_refuses_two_tables_without_a_join_clause(connection):
    """`FROM a, b WHERE a.x = b.y` is a join written the old way, and the
    WHERE is the only thing keeping it from being a cross product."""
    connection.execute(text("CREATE TABLE items (id INTEGER, order_id INTEGER)"))

    found = gather(
        connection,
        question="q",
        sql="SELECT o.id FROM orders o, items i WHERE i.order_id = o.id",
        dialect="sqlite",
        row_count=1,
    )

    assert found.unfiltered_count is None


def test_a_single_table_query_is_still_probed(connection):
    """The check the module exists for must survive the fix."""
    found = gather(
        connection,
        question="orders worth anything",
        sql="SELECT id FROM orders WHERE total > 0",
        dialect="sqlite",
        row_count=3,
    )

    assert found.unfiltered_count == 3
