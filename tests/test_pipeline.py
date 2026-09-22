"""Tests for the agent pipeline.

These use a **scripted fake model** rather than calling Mantle. The point is to
test the orchestration — which loop fires, how many attempts are made, what the
trace records — and that has to be deterministic. A real model might succeed on
the first try, which would never exercise the repair paths.

The database is real, because the error classification that drives the loops
comes from Postgres itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from sqlagent.config import Settings
from sqlagent.llm.mantle import Completion
from sqlagent.pipeline import SqlAgent

pytestmark = pytest.mark.integration


class FakeClient:
    """Returns pre-scripted replies, in order.

    Records every prompt so tests can assert on what the model was actually
    shown — which is how the "did Loop B widen the context?" tests work.
    """

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, model=None, system=None, **kwargs) -> Completion:
        self.prompts.append(prompt)
        text = self.replies.pop(0) if self.replies else "SELECT 1"
        return Completion(
            text=text,
            model=model or "fake",
            input_tokens=10,
            output_tokens=5,
            seconds=0.01,
        )


def build_agent(engine: Engine, replies: list[str], **overrides) -> tuple[SqlAgent, FakeClient]:
    config = Settings(
        database_url=str(engine.url),
        # Sampling is disabled by default here so tests assert on the prompt's
        # schema section without sample rows adding noise.
        sample_rows=overrides.pop("sample_rows", 0),
        # The test schema has 8 tables, which is below the production
        # full-schema threshold — so retrieval would be skipped entirely.
        # Default to forcing retrieval on, since that is what most of these
        # tests exist to exercise. The small-schema shortcut has its own
        # tests, which opt back in explicitly.
        full_schema_threshold=overrides.pop("full_schema_threshold", 0),
        # The product default is `ask_human`, which spends a scripted reply on
        # an ambiguity check before any SQL is written. These tests count model
        # calls and assert on prompts, so they opt out; the clarification tests
        # set it back explicitly.
        ambiguity_handling=overrides.pop("ambiguity_handling", "best_effort"),
        **overrides,
    )
    client = FakeClient(replies)
    return SqlAgent(engine, client=client, config=config), client


SEEDS = '{"tables": ["customers"]}'
SEEDS_ORDERS = '{"tables": ["orders"]}'
ANSWER = "There are 3 customers."


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_answers_a_simple_question(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine, [SEEDS, "SELECT count(*) AS n FROM customers", ANSWER]
    )

    result = agent.ask("How many customers are there?")

    assert result.ok
    assert result.result is not None
    assert result.result.rows[0][0] == 3
    assert result.answer == ANSWER


def test_no_repairs_needed_is_recorded_as_zero(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine, [SEEDS, "SELECT count(*) FROM customers", ANSWER]
    )

    assert agent.ask("How many customers?").trace.repair_count == 0


def test_row_limit_is_applied_to_generated_sql(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine, [SEEDS, "SELECT * FROM customers", ANSWER], row_limit=2
    )

    result = agent.ask("Show me customers")

    assert "LIMIT 2" in (result.sql or "")
    assert result.result is not None
    assert result.result.row_count == 2


def test_trace_accumulates_token_usage(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine, [SEEDS, "SELECT count(*) FROM customers", ANSWER]
    )

    trace = agent.ask("How many customers?").trace

    assert trace.model_calls == 3  # seed selection, generation, answer
    assert trace.input_tokens == 30
    assert trace.output_tokens == 15


def test_empty_result_skips_the_answer_model_call(seeded_engine: Engine):
    """Nothing to summarise, and a model given no rows invents explanations."""
    agent, client = build_agent(
        seeded_engine,
        [SEEDS_ORDERS, "SELECT * FROM orders WHERE total > 999999"],
    )

    result = agent.ask("Show me orders over a million")

    assert result.ok
    assert result.result is not None
    assert result.result.row_count == 0
    assert "no rows" in result.answer
    assert client.prompts and len(client.prompts) == 2  # no answer call


# --------------------------------------------------------------------------
# Loop A: the SQL was wrong
# --------------------------------------------------------------------------


def test_invalid_sql_triggers_regeneration(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine,
        [
            SEEDS,
            "DELETE FROM customers",  # refused by the validator
            "SELECT count(*) FROM customers",  # the repair
            ANSWER,
        ],
    )

    result = agent.ask("How many customers?")

    assert result.ok
    assert result.trace.repair_count == 1
    assert result.trace.attempts[0].error_kind == "invalid_sql"
    assert result.trace.attempts[1].ok


def test_repair_prompt_includes_the_failure_and_the_error(seeded_engine: Engine):
    """Without both, the model tends to reproduce the same mistake."""
    agent, client = build_agent(
        seeded_engine,
        [SEEDS, "SELECT nonexistent_col FROM customers", "SELECT count(*) FROM customers", ANSWER],
    )

    agent.ask("How many customers?")

    repair_prompt = client.prompts[2]
    assert "This query failed:" in repair_prompt
    assert "nonexistent_col" in repair_prompt
    assert "Error:" in repair_prompt


def test_syntax_error_is_repaired(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine,
        [SEEDS, "SELECT FROM WHERE", "SELECT count(*) FROM customers", ANSWER],
    )

    result = agent.ask("How many customers?")

    assert result.ok
    assert result.trace.repair_count == 1


# --------------------------------------------------------------------------
# Loop B: the context was insufficient
# --------------------------------------------------------------------------


def test_missing_table_widens_the_schema_context(seeded_engine: Engine):
    """The distinguishing test between the two loops.

    Starting from ``regions`` at one hop, ``orders`` is two hops away and so is
    not offered. A query needing it fails with ``missing_relation``, which must
    widen the walk rather than simply retrying with identical context.
    """
    agent, _ = build_agent(
        seeded_engine,
        [
            '{"tables": ["regions"]}',
            # Fails: orders is not in the one-hop neighbourhood of regions.
            "SELECT SUM(o.total) FROM orders o",
            "SELECT count(*) FROM regions",
            ANSWER,
        ],
        initial_hops=1,
    )

    result = agent.ask("Total order value by region")

    assert result.trace.hops == 2
    # Caught by the validator's allow-list before any database round trip —
    # the cheapest possible way to learn the context was too narrow.
    assert result.trace.attempts[0].error_kind == "table_not_allowed"


def test_widening_offers_more_tables_to_the_model(seeded_engine: Engine):
    agent, client = build_agent(
        seeded_engine,
        [
            '{"tables": ["regions"]}',
            "SELECT SUM(o.total) FROM orders o",
            "SELECT count(*) FROM regions",
            ANSWER,
        ],
        initial_hops=1,
    )

    agent.ask("Total order value by region")

    first_prompt, repair_prompt = client.prompts[1], client.prompts[2]
    assert "orders" not in first_prompt.split("Question:")[0]
    assert "orders" in repair_prompt.split("Question:")[0]


# --------------------------------------------------------------------------
# Giving up
# --------------------------------------------------------------------------


def test_repeated_failure_gives_up_and_reports_the_last_error(seeded_engine: Engine):
    """An unbounded repair loop would burn tokens indefinitely."""
    agent, _ = build_agent(
        seeded_engine,
        [SEEDS] + ["DELETE FROM customers"] * 10,
        max_repair_attempts=2,
    )

    result = agent.ask("How many customers?")

    assert not result.ok
    assert len(result.trace.attempts) == 3  # first try plus two repairs
    assert "could not produce a working query" in result.answer


def test_failure_message_surfaces_the_actual_error(seeded_engine: Engine):
    """A bare failure is useless; the real error often tells the user the fix."""
    agent, _ = build_agent(
        seeded_engine,
        [SEEDS] + ["SELECT * FROM table_that_does_not_exist"] * 10,
        max_repair_attempts=1,
    )

    result = agent.ask("Show me something")

    assert not result.ok
    assert result.error


# --------------------------------------------------------------------------
# Seed table selection
# --------------------------------------------------------------------------


def test_unparseable_model_reply_falls_back_to_name_matching(seeded_engine: Engine):
    """The agent stays functional when the model returns nonsense."""
    agent, _ = build_agent(
        seeded_engine,
        ["I'm not sure what you mean!", "SELECT count(*) FROM customers", ANSWER],
    )

    result = agent.ask("How many customers are there?")

    assert result.ok
    assert result.trace.seed_tables == ["customers"]


def test_fallback_matches_singular_table_names(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine, ["garbage", "SELECT count(*) FROM customers", ANSWER]
    )

    result = agent.ask("how many customer records exist")

    assert "customers" in result.trace.seed_tables


def test_question_matching_no_table_returns_a_clear_message(seeded_engine: Engine):
    agent, _ = build_agent(seeded_engine, ["garbage"])

    result = agent.ask("what is the weather in paris")

    assert not result.ok
    assert result.error == "no_seed_tables"
    assert "could not match" in result.answer


def test_hallucinated_table_names_are_discarded(seeded_engine: Engine):
    """A model naming a table that does not exist must not break the walk."""
    agent, _ = build_agent(
        seeded_engine,
        [
            '{"tables": ["customers", "imaginary_table"]}',
            "SELECT count(*) FROM customers",
            ANSWER,
        ],
    )

    result = agent.ask("How many customers?")

    assert result.ok
    assert "imaginary_table" not in result.trace.seed_tables


# --------------------------------------------------------------------------
# Context construction
# --------------------------------------------------------------------------


def test_prompt_contains_join_predicates(seeded_engine: Engine):
    """The highest-value line in the prompt: exact join conditions."""
    agent, client = build_agent(
        seeded_engine, [SEEDS, "SELECT count(*) FROM customers", ANSWER]
    )

    agent.ask("How many customers?")

    assert "orders.customer_id = customers.id" in client.prompts[1]


def test_prompt_excludes_unrelated_tables(seeded_engine: Engine):
    """audit_log has no foreign keys and must never appear."""
    agent, client = build_agent(
        seeded_engine, [SEEDS, "SELECT count(*) FROM customers", ANSWER]
    )

    agent.ask("How many customers?")

    assert "audit_log" not in client.prompts[1]


def test_value_profiles_can_be_enabled(seeded_engine: Engine):
    """Opt-in context mode: what each column can contain.

    Off by default — it measured slightly worse than row sampling on BIRD for
    44% more tokens. See Settings.value_profiling.
    """
    agent, client = build_agent(
        seeded_engine,
        [SEEDS, "SELECT count(*) FROM customers", ANSWER],
        sample_rows=2,
        value_profiling=True,
    )

    agent.ask("How many customers?")

    assert "Column values" in client.prompts[1]
    assert "Ada Lovelace" in client.prompts[1]


def test_low_cardinality_columns_are_listed_exhaustively(seeded_engine: Engine):
    """The point of profiling: a filter needs the vocabulary, not a specimen row.

    ``regions.iso_code`` holds only GB and DE in the fixture, so both should be
    offered without an "e.g." hedge.
    """
    agent, client = build_agent(
        seeded_engine,
        ['{"tables": ["regions"]}', "SELECT count(*) FROM regions", ANSWER],
        sample_rows=2,
        value_profiling=True,
    )

    agent.ask("How many regions?")

    prompt = client.prompts[1]
    assert "'GB'" in prompt
    assert "'DE'" in prompt


def test_row_sampling_is_the_default(seeded_engine: Engine):
    agent, client = build_agent(
        seeded_engine,
        [SEEDS, "SELECT count(*) FROM customers", ANSWER],
        sample_rows=2,
    )

    agent.ask("How many customers?")

    assert "Example rows:" in client.prompts[1]
    assert "Ada Lovelace" in client.prompts[1]


def test_sampling_can_be_disabled_entirely(seeded_engine: Engine):
    """Required for tenants who will not allow row data to leave the database."""
    agent, client = build_agent(
        seeded_engine,
        [SEEDS, "SELECT count(*) FROM customers", ANSWER],
        sample_rows=0,
    )

    result = agent.ask("How many customers?")

    assert result.ok
    assert "Example rows:" not in client.prompts[1]


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


def test_markdown_fenced_sql_is_accepted(seeded_engine: Engine):
    """Some models fence their output no matter what the prompt says."""
    agent, _ = build_agent(
        seeded_engine,
        [SEEDS, "```sql\nSELECT count(*) FROM customers\n```", ANSWER],
    )

    assert agent.ask("How many customers?").ok


# --------------------------------------------------------------------------
# Small-schema shortcut
# --------------------------------------------------------------------------


def test_small_schema_skips_seed_selection(seeded_engine: Engine):
    """Below the threshold, show the model everything and save a model call.

    Measured on BIRD, guessing a seed table wrongly caused about a quarter of
    all failures on databases whose entire schema would have fitted anyway.
    """
    agent, client = build_agent(
        seeded_engine,
        ["SELECT count(*) FROM customers", ANSWER],
        full_schema_threshold=15,
    )

    result = agent.ask("How many customers are there?")

    assert result.ok
    # Two calls, not three: no seed-selection round trip.
    assert result.trace.model_calls == 2


def test_small_schema_offers_every_table(seeded_engine: Engine):
    agent, client = build_agent(
        seeded_engine,
        ["SELECT count(*) FROM customers", ANSWER],
        full_schema_threshold=15,
    )

    agent.ask("How many customers?")

    schema_section = client.prompts[0].split("Question:")[0]
    for table in ("customers", "orders", "regions", "audit_log", "employees"):
        assert table in schema_section


def test_small_schema_answers_a_question_needing_a_distant_table(seeded_engine: Engine):
    """The failure mode the shortcut removes.

    order_items is two hops from customers, so one-hop retrieval would not
    offer it. With the whole schema available the query simply works.
    """
    agent, _ = build_agent(
        seeded_engine,
        [
            "SELECT count(*) FROM order_items oi "
            "JOIN orders o ON oi.order_id = o.id "
            "JOIN customers c ON o.customer_id = c.id",
            ANSWER,
        ],
        full_schema_threshold=15,
    )

    result = agent.ask("How many order items belong to customers?")

    assert result.ok
    assert result.trace.repair_count == 0


def test_threshold_of_zero_always_uses_retrieval(seeded_engine: Engine):
    agent, _ = build_agent(
        seeded_engine,
        [SEEDS, "SELECT count(*) FROM customers", ANSWER],
        full_schema_threshold=0,
    )

    result = agent.ask("How many customers?")

    assert result.trace.model_calls == 3  # seed selection happened
    assert result.trace.seed_tables == ["customers"]


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


def test_model_failure_is_returned_not_raised(seeded_engine: Engine):
    """Callers are web handlers and benchmark runners; both want a result."""

    class ExplodingClient(FakeClient):
        def complete(self, prompt, **kwargs):
            raise RuntimeError("model is on fire")

    config = Settings(database_url=str(seeded_engine.url), sample_rows=0)
    agent = SqlAgent(seeded_engine, client=ExplodingClient([]), config=config)

    result = agent.ask("How many customers?")

    assert not result.ok
    assert "went wrong" in result.answer
