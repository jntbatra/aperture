"""End-to-end tests with each optional feature switched on.

Why these exist
---------------
Every toggle here defaults to off, which means the unit tests for each one test
the module in isolation while the *wiring* — the graph node, the routing, the
import — is never executed by the suite at all.

That is not hypothetical. Two imports in ``agent_graph`` were missing for a
while and every test passed, because the code that used them only runs when a
flag nobody had flipped is on. A linter caught it; the tests should have.

So each test here turns one flag on and drives a real question through the real
graph against a real SQLite database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from sqlagent.clarify import CLARIFICATION_ERROR
from sqlagent.config import Settings, critic_enabled, vote_samples
from sqlagent.llm.mantle import Completion
from sqlagent.pipeline import SqlAgent


class ScriptedClient:
    """Replies in order, then repeats the last one.

    Scripted rather than pattern-matched: these tests are about which calls
    happen and in what order, and a stub that answers by inspecting the prompt
    hides exactly that.
    """

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str | None] = []
        self.temperatures: list[float | None] = []

    def complete(self, prompt, *, model=None, system=None, temperature=None):
        index = min(len(self.prompts), len(self._replies) - 1)
        self.prompts.append(prompt)
        self.systems.append(system)
        self.temperatures.append(temperature)
        return Completion(
            text=self._replies[index],
            input_tokens=10,
            output_tokens=5,
            model=model or "m",
            seconds=0.01,
        )

    @property
    def calls(self) -> int:
        return len(self.prompts)


@pytest.fixture
def database(tmp_path):
    """A two-table SQLite database, small enough to skip retrieval entirely."""
    path = tmp_path / "toggles.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER "
                "REFERENCES customers(id), total INTEGER)"
            )
        )
        connection.execute(text("INSERT INTO customers VALUES (1, 'Ada'), (2, 'Bo')"))
        connection.execute(text("INSERT INTO orders VALUES (1, 1, 400), (2, 2, 300)"))
    return engine


def build(database, client, **overrides) -> SqlAgent:
    # Defaults chosen so each test exercises its own toggle and nothing else:
    # every table always in context, no sampling, and the unrelated checks off.
    base = {
        "database_url": "sqlite://",
        "full_schema_threshold": 15,
        "sample_rows": 0,
        "cache_sql": False,
        "check_answer_faithfulness": False,
        "max_plan_cost": 0,
        "max_plan_rows": 0,
        "check_inflated_aggregates": False,
        # The product default is now `ask_human`, which would consume a scripted
        # reply on every question. Tests about *other* toggles opt out; the two
        # that are about clarification set it back explicitly.
        "ambiguity_handling": "best_effort",
    }
    # An override of None means "drop the test default and use whatever the
    # product ships", which is how a test asserts on a real default.
    merged = dict(base)
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    # _env_file=None so a developer's own .env cannot change what these tests
    # exercise — the conftest fixture clears the variables, this closes the file.
    config = Settings(_env_file=None, **merged)
    agent = SqlAgent(database, client=client, config=config)
    agent.warm()
    return agent


# --------------------------------------------------------------------------
# The critic
# --------------------------------------------------------------------------


def test_the_critic_can_reject_a_query_and_force_a_rewrite(database):
    """Loop C end to end: generate, reject with a named defect, regenerate."""
    client = ScriptedClient(
        "SELECT count(*) FROM orders",                       # first attempt
        '{"ok": false, "problem": "counts orders, but the question asks '
        'about customers"}',                                  # critic rejects
        "SELECT count(*) FROM customers",                     # rewrite
        '{"ok": true}',                                       # critic approves
        "There are 2 customers.",                             # answer
    )
    agent = build(database, client, use_critic=True)

    result = agent.ask("How many customers are there?")

    assert result.ok
    # `result.sql` is the validated statement — normalised, with a row cap
    # appended — so the table it reads is what to assert on.
    assert "customers" in result.sql and "orders" not in result.sql
    assert result.trace.critic_rejections == [
        "counts orders, but the question asks about customers"
    ]


def test_the_rejection_reaches_the_repair_prompt(database):
    """Told only "that is wrong", a model rewrites and keeps the mistake."""
    client = ScriptedClient(
        "SELECT count(*) FROM orders",
        '{"ok": false, "problem": "wrong table entirely"}',
        "SELECT count(*) FROM customers",
        '{"ok": true}',
        "There are 2 customers.",
    )
    agent = build(database, client, use_critic=True)

    agent.ask("How many customers?")

    assert any("wrong table entirely" in prompt for prompt in client.prompts)


def test_the_critic_is_not_called_when_it_is_off(database):
    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client, use_critic=False)

    agent.ask("How many customers?")

    assert client.calls == 2  # generate, answer — no review


def test_an_approving_critic_does_not_loop(database):
    """`failure_kind` left set from a previous pass would route an approved
    query straight back to be rewritten, forever."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers", '{"ok": true}', "There are 2 customers."
    )
    agent = build(database, client, use_critic=True)

    result = agent.ask("How many customers?")

    assert result.ok
    assert result.trace.repair_count == 0


# --------------------------------------------------------------------------
# Voting
# --------------------------------------------------------------------------


def test_voting_generates_several_candidates_and_keeps_the_majority(database):
    client = ScriptedClient(
        "SELECT count(*) FROM orders",
        "SELECT count(*) FROM customers",
        "SELECT count(*) FROM customers",
        "There are 2 customers.",
    )
    agent = build(database, client, vote_samples=3)

    result = agent.ask("How many customers?")

    assert "customers" in result.sql and "orders" not in result.sql
    assert result.trace.vote_agreement == 2
    assert result.trace.vote_samples == 3


def test_voting_raises_the_temperature_for_its_samples_only(database):
    """At temperature 0 three samples are three identical strings and three
    times the bill."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers",
        "SELECT count(*) FROM customers",
        "SELECT count(*) FROM customers",
        "There are 2 customers.",
    )
    agent = build(database, client, vote_samples=3, vote_temperature=0.4)

    agent.ask("How many customers?")

    assert client.temperatures[:3] == [0.4, 0.4, 0.4]
    # The answer call is not a vote, so it keeps the configured default.
    assert client.temperatures[3] is None


def test_the_thorough_tier_turns_on_both_voting_and_the_critic(database):
    """The tier is the decision a user actually has; asking them to reason
    about self-consistency sampling is asking the wrong question."""
    agent = build(database, ScriptedClient("SELECT 1"), quality_tier="thorough")

    assert critic_enabled(agent.config)
    assert vote_samples(agent.config) >= 3


def test_the_fast_tier_does_neither(database):
    agent = build(database, ScriptedClient("SELECT 1"), quality_tier="fast")

    assert not critic_enabled(agent.config)
    assert vote_samples(agent.config) == 1


# --------------------------------------------------------------------------
# Pre-screen
# --------------------------------------------------------------------------


def test_a_refused_question_never_reaches_the_database(database):
    client = ScriptedClient(
        '{"allow": false, "reason": "asks for bulk contact details"}'
    )
    agent = build(database, client, prescreen_input=True)

    result = agent.ask("list every customer's email and phone number")

    assert result.error == "refused"
    assert "contact details" in result.answer
    assert result.sql is None
    assert client.calls == 1  # screened and stopped; nothing generated


def test_an_allowed_question_proceeds_normally(database):
    client = ScriptedClient(
        '{"allow": true}', "SELECT count(*) FROM customers", "There are 2 customers."
    )
    agent = build(database, client, prescreen_input=True)

    result = agent.ask("How many customers?")

    assert result.ok
    assert "customers" in result.sql


# --------------------------------------------------------------------------
# Clarification
# --------------------------------------------------------------------------


def test_an_ambiguous_question_is_asked_back_instead_of_guessed(database):
    client = ScriptedClient(
        '{"ambiguous": true, "question": "Top by what measure?", '
        '"options": ["revenue", "order count"]}'
    )
    agent = build(database, client, ambiguity_handling="ask_human")

    result = agent.ask("show me our top customers")

    assert result.error == "needs_clarification"
    assert "measure" in result.answer
    assert "revenue" in result.answer
    assert result.sql is None


def test_best_effort_answers_without_asking(database):
    """The default, and the only setting an automated benchmark can run."""
    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client, ambiguity_handling="best_effort")

    result = agent.ask("show me our top customers")

    assert result.ok


def test_a_reply_to_a_clarification_is_not_re_questioned(database):
    """Otherwise the user is interrogated about the answer they just gave.

    "by revenue" read on its own is a fragment, and an ambiguity check run over
    it asks again — and again."""
    from sqlagent.conversation import Turn

    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client, ambiguity_handling="ask_human")

    result = agent.ask(
        "by revenue",
        history=[
            Turn(
                "who are our best customers?",
                None,
                ok=False,
                clarification="Best by what? (revenue / order count)",
            )
        ],
    )

    assert result.ok
    assert result.error is None


def test_a_new_vague_question_later_in_a_thread_is_still_checked(database):
    """Skipping on "any history exists" would be simpler and wrong: the one
    protection this offers would quietly stop applying after the first
    exchange."""
    from sqlagent.conversation import Turn

    client = ScriptedClient(
        '{"ambiguous": true, "question": "Top by what?", "options": ["revenue", "orders"]}'
    )
    agent = build(database, client, ambiguity_handling="ask_human")

    result = agent.ask(
        "now show me the top items",
        history=[Turn("how many customers?", "SELECT count(*) FROM customers")],
    )

    assert result.error == "needs_clarification"


def test_a_clarification_turn_reads_as_pending_not_failed(database):
    """It has no SQL, so without the marker the history renders it as "that
    question could not be answered" — the opposite of what happened."""
    from sqlagent.conversation import Turn, render_conversation

    text = render_conversation(
        [Turn("who are our best customers?", None, ok=False,
              clarification="Best by what? (revenue / orders)")]
    )

    assert "could not be answered" not in text
    assert "I asked back" in text
    assert "reply to that" in text


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


def test_an_identical_question_reuses_the_sql_but_re_runs_the_query(database):
    """The statement is cached; the rows are not. The answer stays fresh."""
    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client, cache_sql=True)

    first = agent.ask("How many customers?")
    calls_after_first = client.calls

    second = agent.ask("how many customers")  # same question, different casing

    assert second.sql == first.sql
    assert second.trace.cache_hit
    # One further call — the answer. Generation was skipped.
    assert client.calls == calls_after_first + 1
    assert second.result.rows == first.result.rows


def test_a_follow_up_is_never_served_from_the_cache(database):
    """"And for April?" means whatever the previous turns made it mean."""
    from sqlagent.conversation import Turn

    client = ScriptedClient("SELECT count(*) FROM customers", "Two.")
    agent = build(database, client, cache_sql=True)

    agent.ask("How many customers?")
    result = agent.ask(
        "How many customers?", history=[Turn("something else", "SELECT 1")]
    )

    assert not result.trace.cache_hit


def test_a_cached_statement_that_fails_is_dropped(database):
    """Serving it again would repeat the failure, and the repair path would keep
    starting from a statement known to be broken."""
    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client, cache_sql=True)

    key = agent.cache.key(
        "How many customers?", schema_version=agent.snapshot.version, glossary=""
    )
    agent.cache.put(key, "SELECT count(*) FROM no_such_table")

    result = agent.ask("How many customers?")

    # The broken statement is gone, and the question was answered anyway by
    # falling back to the full path.
    assert agent.cache.get(key) != "SELECT count(*) FROM no_such_table"
    assert result.ok


# --------------------------------------------------------------------------
# Decomposition
# --------------------------------------------------------------------------


def test_a_multi_part_question_is_answered_in_parts(database):
    """The failure this addresses: asked for seven things, the agent answered
    one and said nothing about the other six."""
    client = ScriptedClient(
        '{"parts": ["How many customers?", "How many orders?"]}',  # decompose
        "SELECT count(*) FROM customers",                           # part 1 SQL
        "There are 2 customers.",                                   # part 1 answer
        "SELECT count(*) FROM orders",                              # part 2 SQL
        "There are 2 orders.",                                      # part 2 answer
        "2 customers placed 2 orders.",                             # synthesis
    )
    agent = build(database, client, decompose_questions=True)

    result = agent.ask("how many customers and how many orders?")

    assert result.answer == "2 customers placed 2 orders."
    assert result.trace.parts == ["How many customers?", "How many orders?"]


def test_a_single_part_question_takes_the_normal_path(database):
    client = ScriptedClient(
        '{"parts": []}', "SELECT count(*) FROM customers", "There are 2 customers."
    )
    agent = build(database, client, decompose_questions=True)

    result = agent.ask("How many customers?")

    assert result.trace.parts == []
    assert "customers" in result.sql


def test_each_part_is_validated_like_any_other_question(database):
    """A sub-question is just a question. Giving it a reduced pipeline would
    mean two code paths with two sets of guarantees."""
    client = ScriptedClient(
        '{"parts": ["How many customers?", "DROP TABLE customers"]}',
        "SELECT count(*) FROM customers",
        "There are 2 customers.",
        "DROP TABLE customers",          # part 2 tries a write
        "DROP TABLE customers",          # and again on repair
        "DROP TABLE customers",
        "DROP TABLE customers",
        "Only the first part could be answered.",
    )
    agent = build(database, client, decompose_questions=True)

    agent.ask("how many customers, and drop the table")

    # The table is still there: the validator rejected the write inside the
    # sub-question exactly as it would at the top level.
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM customers")).scalar() == 2


def test_a_follow_up_is_never_decomposed(database):
    """A follow-up refines something; it does not open a new investigation."""
    from sqlagent.conversation import Turn

    client = ScriptedClient("SELECT count(*) FROM customers", "Two.")
    agent = build(database, client, decompose_questions=True)

    result = agent.ask(
        "and the orders?", history=[Turn("how many customers?", "SELECT 1")]
    )

    assert result.trace.parts == []


# --------------------------------------------------------------------------
# Per-request overrides
#
# The toggles are surfaced in the interface, so they have to be settable per
# question rather than only per deployment.
# --------------------------------------------------------------------------


def test_a_request_can_turn_the_critic_on(database):
    """The server default is off; this question asks for it anyway."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers", '{"ok": true}', "There are 2 customers."
    )
    agent = build(database, client, use_critic=False)

    agent.ask("How many customers?", options={"use_critic": True})

    assert client.calls == 3  # generate, review, answer


def test_a_request_can_turn_the_critic_off(database):
    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client, use_critic=True)

    agent.ask("How many customers?", options={"use_critic": False})

    assert client.calls == 2  # no review


def test_the_thorough_tier_can_be_asked_for_per_question(database):
    client = ScriptedClient(
        "SELECT count(*) FROM customers",
        "SELECT count(*) FROM customers",
        "SELECT count(*) FROM customers",
        '{"ok": true}',
        "There are 2 customers.",
    )
    agent = build(database, client, quality_tier="fast")

    result = agent.ask("How many customers?", options={"quality_tier": "thorough"})

    assert result.trace.vote_samples == 3


def test_overrides_do_not_leak_into_the_next_question(database):
    """One careful question must not make every later question careful — the
    settings object the agent holds is never mutated."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers",   # first question: generate
        '{"ok": true}',                      # first question: review
        "There are 2 customers.",            # first question: answer
        "SELECT count(*) FROM orders",       # second question: generate
        "There are 2 orders.",               # second question: answer
    )
    agent = build(database, client, use_critic=False)

    agent.ask("How many customers?", options={"use_critic": True})
    calls_after_thorough = client.calls

    agent.ask("How many orders?")

    assert client.calls == calls_after_thorough + 2  # generate, answer — no review
    assert agent.config.use_critic is False


def test_a_setting_outside_the_allow_list_is_ignored(database):
    """An allow-list fails closed. A client that could set its own row cap or
    database URL would be removing a control, not configuring a feature."""
    client = ScriptedClient("SELECT count(*) FROM customers", "Two.")
    agent = build(database, client)

    agent.ask("How many customers?", options={"row_limit": 999_999})

    assert agent.config.row_limit == 1000


def test_no_options_changes_nothing(database):
    client = ScriptedClient("SELECT count(*) FROM customers", "Two.")
    agent = build(database, client)

    result = agent.ask("How many customers?", options=None)

    assert result.ok


def test_asking_is_the_default_so_nothing_is_supposed(database):
    """The reversal: a wrong assumption presented as an answer is worse than a
    question. Observed — "who are our best customers lately" silently decided
    'best' meant revenue and dropped 'lately' entirely."""
    client = ScriptedClient(
        '{"ambiguous": true, "question": "Best by what?", "options": ["revenue", "orders"]}'
    )
    # No ambiguity_handling override: this asserts the shipped default.
    agent = build(database, client, ambiguity_handling=None)

    result = agent.ask("who are our best customers lately?")

    assert result.error == "needs_clarification"
    assert result.sql is None


def test_decomposition_does_not_bypass_the_clarifying_question(database):
    """Decomposition runs above the graph, so it used to skip screening
    entirely: a vague question was split into vague parts, each answered badly,
    and synthesised into content-free prose. Splitting a question nobody has
    pinned down multiplies the guessing rather than removing it."""
    client = ScriptedClient(
        '{"ambiguous": true, "question": "Best by what?", "options": ["revenue", "orders"]}'
    )
    agent = build(
        database, client, decompose_questions=True, ambiguity_handling="ask_human"
    )

    result = agent.ask("who are our best customers lately?")

    assert result.error == "needs_clarification"
    assert result.trace.parts == []      # nothing was split
    assert client.calls == 1             # and nothing was decomposed


def test_a_clear_question_still_decomposes_with_asking_on(database):
    client = ScriptedClient(
        '{"ambiguous": false}',                                   # screened, fine
        '{"parts": ["How many customers?", "How many orders?"]}',  # then split
        "SELECT count(*) FROM customers", "Two customers.",
        "SELECT count(*) FROM orders", "Two orders.",
        "Two customers placed two orders.",
    )
    agent = build(
        database, client, decompose_questions=True, ambiguity_handling="ask_human"
    )

    result = agent.ask("how many customers and how many orders?")

    assert result.trace.parts == ["How many customers?", "How many orders?"]


def test_generated_parts_are_never_questioned_back(database):
    """The parts are machine-generated from a question the user was already
    asked about. Interrogating them about wording they never wrote would stall
    the run once per part."""
    client = ScriptedClient(
        '{"ambiguous": false}',
        '{"parts": ["How many customers?", "How many orders?"]}',
        "SELECT count(*) FROM customers", "Two customers.",
        "SELECT count(*) FROM orders", "Two orders.",
        "Two and two.",
    )
    agent = build(
        database, client, decompose_questions=True, ambiguity_handling="ask_human"
    )

    result = agent.ask("how many customers and how many orders?")

    assert result.ok
    assert len(result.trace.parts) == 2


def test_schema_breadth_is_settable_per_question(database):
    """How many tables the model is shown is a per-question judgement: more
    tables means less chance of missing one it needed, more chance of being
    distracted by one it did not."""
    # full_schema_threshold=0 forces retrieval on, so seed selection runs and
    # consumes the first reply.
    client = ScriptedClient(
        '{"tables": ["customers"]}',
        "SELECT count(*) FROM customers",
        "There are 2 customers.",
    )
    agent = build(database, client, full_schema_threshold=0)

    result = agent.ask("How many customers?", options={"initial_hops": 2})

    assert result.ok
    assert agent.config.initial_hops == 1   # the agent's own setting is untouched


def test_hops_are_bounded_by_the_request_model():
    """A client walking the whole graph is a prompt-size problem, not a
    preference."""
    import pydantic

    from sqlagent.api.app import AskOptions

    with pytest.raises(pydantic.ValidationError):
        AskOptions(initial_hops=99)


def test_a_later_part_can_see_what_earlier_parts_returned(database):
    """Reported from the running app. "Get the latest order" followed by
    "details of the customer who placed that order" is two parts where the
    second cannot be answered alone. With no context, the model wrote
    `WHERE id = '0c5eec76-...'` — a real id belonging to the *oldest* order,
    cancelled in May — and the answer spliced correct order details onto the
    wrong customer.
    """
    client = ScriptedClient(
        '{"parts": ["Which is the newest customer?", "How many orders did that customer place?"]}',
        "SELECT id, name FROM customers ORDER BY id DESC LIMIT 1",
        "The newest customer is Bo.",
        "SELECT count(*) FROM orders WHERE customer_id = 2",
        "Bo placed 1 order.",
        "Bo is the newest customer and placed 1 order.",
    )
    agent = build(database, client, decompose_questions=True)

    agent.ask("who is the newest customer and how many orders did they place?")

    # The prompt for part 2 must carry part 1's finding, or "that customer" has
    # no referent and the model invents an id to bridge the gap.
    second_part_prompts = [p for p in client.prompts if "How many orders did that customer" in p]
    assert second_part_prompts
    assert any("Bo" in p or "SELECT id, name FROM customers" in p for p in second_part_prompts)


def test_the_splitter_is_told_not_to_split_a_single_join(database):
    """The reported question was one query over orders, users and
    customer_profiles. Splitting it turned one correct answer into two that had
    to be stitched back together — and the stitching went wrong."""
    from sqlagent.report import DECOMPOSE_SYSTEM_PROMPT

    assert "ONE query with joins" in DECOMPOSE_SYSTEM_PROMPT
    assert "Do not invent an id" in DECOMPOSE_SYSTEM_PROMPT


# --------------------------------------------------------------------------
# Conversation summarisation
#
# The unit tests in test_summarise.py exercise the folding. These exercise the
# wiring: that the summary is computed once per question rather than once per
# node, that it reaches the prompts, and that a short conversation still costs
# nothing.
# --------------------------------------------------------------------------


def long_history(count: int) -> list:
    from sqlagent.conversation import Turn

    return [Turn(f"question {i}", f"SELECT {i}") for i in range(count)]


def test_a_short_conversation_is_not_summarised(database):
    """Nothing beyond the window means no model call — which is every first
    question, every benchmark question and every CLI invocation."""
    client = ScriptedClient("SELECT count(*) FROM orders", "There are 2 orders.")
    agent = build(database, client, summarise_conversation=True)

    agent.ask("how many orders?", history=long_history(4))

    assert all("Rewrite the running notes" not in p for p in client.prompts)


def test_turns_beyond_the_window_are_summarised(database):
    client = ScriptedClient(
        "Delivered orders only, excluding test accounts.",  # the summary
        "SELECT count(*) FROM orders",
        "There are 2 orders.",
    )
    agent = build(database, client, summarise_conversation=True)

    agent.ask("how many orders?", history=long_history(7))

    assert "Rewrite the running notes" in client.prompts[0]


def test_the_summary_reaches_the_generation_prompt(database):
    """The whole point. A constraint set in turn 1 has to still constrain the
    query written in turn 8."""
    client = ScriptedClient(
        "Delivered orders only, excluding test accounts.",
        "SELECT count(*) FROM orders",
        "There are 2 orders.",
    )
    agent = build(database, client, summarise_conversation=True)

    agent.ask("how many orders?", history=long_history(7))

    generation = [p for p in client.prompts if "how many orders?" in p]
    assert generation
    assert any("excluding test accounts" in p for p in generation)


def test_the_summary_is_computed_once_per_question_not_once_per_node(database):
    """Three nodes render the conversation. Each summarising again would
    triple the cost of the one feature justified by being cheap."""
    client = ScriptedClient(
        "Delivered orders only.",
        "SELECT count(*) FROM orders",
        "There are 2 orders.",
    )
    agent = build(database, client, summarise_conversation=True)

    agent.ask("how many orders?", history=long_history(7))

    assert len([p for p in client.prompts if "Rewrite the running notes" in p]) == 1


def test_a_second_question_reuses_the_summary(database):
    """Same evicted turns, no second summarisation — otherwise re-opening a
    long chat re-summarises it on every question."""
    client = ScriptedClient(
        "Delivered orders only.",
        "SELECT count(*) FROM orders",
        "There are 2 orders.",
    )
    agent = build(database, client, summarise_conversation=True)
    history = long_history(7)

    agent.ask("how many orders?", history=history)
    before = len([p for p in client.prompts if "Rewrite the running notes" in p])
    agent.ask("and how many customers?", history=history)
    after = len([p for p in client.prompts if "Rewrite the running notes" in p])

    assert before == after == 1


def test_switching_summarisation_off_restores_the_bare_window(database):
    client = ScriptedClient("SELECT count(*) FROM orders", "There are 2 orders.")
    agent = build(database, client, summarise_conversation=False)

    agent.ask("how many orders?", history=long_history(9))

    assert all("Rewrite the running notes" not in p for p in client.prompts)


def test_a_wider_window_evicts_less(database):
    """`conversation_window` is overridable, so it has to actually move the
    boundary the summariser uses — not just the one the renderer uses."""
    client = ScriptedClient("SELECT count(*) FROM orders", "There are 2 orders.")
    agent = build(database, client, summarise_conversation=True, conversation_window=20)

    agent.ask("how many orders?", history=long_history(9))

    assert all("Rewrite the running notes" not in p for p in client.prompts)


def test_a_summarisation_failure_still_answers(database):
    """A side channel must not be able to break answering."""

    class FailsToSummarise(ScriptedClient):
        def complete(self, prompt, *, model=None, system=None, temperature=None):
            if "Rewrite the running notes" in prompt:
                raise RuntimeError("model unavailable")
            return super().complete(prompt, model=model, system=system, temperature=temperature)

    client = FailsToSummarise("SELECT count(*) FROM orders", "There are 2 orders.")
    agent = build(database, client, summarise_conversation=True)

    result = agent.ask("how many orders?", history=long_history(7))

    assert result.ok


# --------------------------------------------------------------------------
# How many things to ask about at once
# --------------------------------------------------------------------------


FOUR_ASKS = (
    '{"ambiguous": true, "asks": ['
    '{"question": "Best by what?", "options": ["revenue", "orders"]},'
    '{"question": "Lately means?", "options": ["30 days", "quarter"]},'
    '{"question": "Doing how?", "options": ["spend", "frequency"]},'
    '{"question": "Compared to when?", "options": ["last year", "last quarter"]}]}'
)


def test_all_four_ambiguities_reach_the_user(database):
    """The cap was 3, and this question has four. The fourth was silently
    invented — the exact failure the check exists to prevent."""
    agent = build(database, ScriptedClient(FOUR_ASKS), ambiguity_handling="ask_human")

    result = agent.ask("who are our best customers lately and how are they doing "
                       "compared to last year?")

    assert len(result.clarification_asks) == 4


def test_lowering_the_cap_shortens_the_exchange(database):
    agent = build(
        database,
        ScriptedClient(FOUR_ASKS),
        ambiguity_handling="ask_human",
        max_clarifying_questions=2,
    )

    result = agent.ask("who are our best customers lately?")

    assert len(result.clarification_asks) == 2


def test_the_cap_reaches_the_prompt_not_just_the_truncation(database):
    """Otherwise the model is told one number and held to another."""
    client = ScriptedClient(FOUR_ASKS)
    agent = build(
        database, client, ambiguity_handling="ask_human", max_clarifying_questions=5
    )

    agent.ask("who are our best customers?")

    assert any(system and "5." in system for system in client.systems)


# --------------------------------------------------------------------------
# The intent check (Loop D)
#
# The only check that holds the question, the SQL and the rows at once, which
# is where 61 of 62 measured failures live. These drive it through the real
# graph against the real SQLite database — the wiring, not the module.
# --------------------------------------------------------------------------


def test_the_intent_check_can_reject_a_result_and_force_a_rewrite(database):
    """Loop D end to end. The query ran, returned rows, and answered the
    wrong question — no error, nothing an earlier check could have caught."""
    client = ScriptedClient(
        "SELECT count(*) FROM orders",                          # first attempt
        '{"verdict": "mismatch", "reason": "it counts orders, but the '
        'question asks how many customers"}',                   # rows seen, rejected
        "SELECT count(*) FROM customers",                       # rewrite
        "There are 2 customers.",                               # answer
    )
    agent = build(database, client, check_result_intent=True)

    result = agent.ask("How many customers are there?")

    assert result.ok
    assert "customers" in result.sql and "orders" not in result.sql
    assert result.trace.intent_rejections == [
        "it counts orders, but the question asks how many customers"
    ]


def test_the_intent_check_can_be_turned_on_for_one_question(database):
    """The SDK's `expensive` tier. Off on the server, on for this question only,
    and off again for the next one — a per-request choice, not a deployment."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers", '{"verdict": "answers"}', "There are 2.",
        "SELECT count(*) FROM customers", "There are 2.",
    )
    agent = build(database, client)

    agent.ask("How many customers?", options={"check_result_intent": True})
    assert client.calls == 3

    agent.ask("How many customers?")
    assert client.calls == 5


def test_the_intent_check_sees_the_rows_not_just_the_sql(database):
    """The critic's whole defect. It reads code and guesses; on the worst
    failure seen on real data it approved the query, and what gave it away was
    the result."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers", '{"verdict": "answers"}', "There are 2."
    )
    agent = build(database, client, check_result_intent=True)

    agent.ask("How many customers?")

    judged = client.prompts[1]
    assert "How many customers?" in judged
    assert "count" in judged.lower()
    assert "2" in judged


def test_the_named_defect_reaches_the_repair_prompt(database):
    client = ScriptedClient(
        "SELECT count(*) FROM orders",
        '{"verdict": "mismatch", "reason": "wrong table entirely"}',
        "SELECT count(*) FROM customers",
        "There are 2 customers.",
    )
    agent = build(database, client, check_result_intent=True)

    agent.ask("How many customers?")

    assert any("wrong table entirely" in prompt for prompt in client.prompts)


def test_an_accepted_result_does_not_loop(database):
    """`failure_kind` left set from the previous pass would route an accepted
    result back to be rewritten, forever. The critic shipped that bug once."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers", '{"verdict": "answers"}', "There are 2."
    )
    agent = build(database, client, check_result_intent=True)

    result = agent.ask("How many customers?")

    assert result.ok
    assert result.trace.repair_count == 0


def test_only_one_rewrite_is_driven_by_intent(database):
    """A database error either stops recurring or does not. "These rows do not
    answer the question" can be said about every rewrite in turn, so it gets
    one go rather than a share of the general repair budget."""
    client = ScriptedClient(
        "SELECT count(*) FROM orders",
        '{"verdict": "mismatch", "reason": "wrong table"}',
        "SELECT count(*) FROM customers",
        "There are 2 customers.",
    )
    agent = build(database, client, check_result_intent=True)

    result = agent.ask("How many customers?")

    assert result.ok
    # Four calls: generate, judge, regenerate, answer. The second result is
    # not judged again, so nothing consumed a fifth reply.
    assert client.calls == 4
    assert len(result.trace.intent_rejections) == 1


def test_a_failed_intent_check_still_returns_the_answer(database):
    """The last thing between a working result and the user. A check that
    cannot parse its own reply must not withhold a correct answer."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers",
        "looks fine to me",            # not JSON, not a verdict
        "There are 2 customers.",
    )
    agent = build(database, client, check_result_intent=True)

    result = agent.ask("How many customers?")

    assert result.ok
    assert result.answer == "There are 2 customers."


def test_the_intent_check_is_not_called_when_it_is_off(database):
    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client, check_result_intent=False)

    agent.ask("How many customers?")

    assert client.calls == 2  # generate, answer — nothing judged


def test_the_intent_check_can_hand_the_question_back(database):
    """The outcome a benchmark cannot reward — a harness has nobody to ask, so
    every ask scores as a failure. Guessing silently is what produced the
    failures this check exists for."""
    client = ScriptedClient(
        '{"ambiguous": false}',                                  # pre-screen
        "SELECT count(*) FROM customers",
        '{"verdict": "ask", "question": "customers or accounts?", '
        '"options": ["customers", "accounts"]}',
    )
    agent = build(
        database, client, check_result_intent=True, ambiguity_handling="ask_human"
    )

    result = agent.ask("How many customers?")

    assert result.error == CLARIFICATION_ERROR
    assert "customers or accounts?" in result.answer
    assert result.clarification_asks == [
        {"question": "customers or accounts?", "options": ["customers", "accounts"]}
    ]


def test_it_never_asks_when_the_deployment_says_not_to(database):
    """`best_effort` means never interrupt the user. A check that asks anyway
    is a setting that does not hold."""
    client = ScriptedClient(
        "SELECT count(*) FROM customers",
        '{"verdict": "ask", "question": "customers or accounts?"}',
        "There are 2 customers.",
    )
    agent = build(
        database, client, check_result_intent=True, ambiguity_handling="best_effort"
    )

    result = agent.ask("How many customers?")

    assert result.ok
    assert result.trace.intent_asks == []
    # …but it wanted to, and a run that cannot say so reports a setting as a
    # finding about the model.
    assert result.trace.intent_asks_withheld == ["customers or accounts?"]


def test_established_facts_are_handed_over_rather_than_left_to_be_noticed(database):
    """The evidence probes run against the real database and their findings go
    into the prompt as settled, before the rows."""
    client = ScriptedClient(
        "SELECT id FROM orders WHERE total > 0",
        '{"verdict": "answers"}',
        "Both orders.",
    )
    agent = build(database, client, check_result_intent=True)

    agent.ask("Which orders are worth anything?")

    judged = client.prompts[1]
    assert "these are facts, not guesses" in judged
    assert "excluded nothing" in judged


# --------------------------------------------------------------------------
# Literal rebinding
# --------------------------------------------------------------------------


def test_an_absent_literal_is_rebound_without_a_model_call(database):
    """The query runs, returns nothing, and nothing was the answer the user
    got. The database can settle it; no model is asked."""
    with database.begin() as connection:
        connection.execute(text("INSERT INTO customers VALUES (3, 'Cravings Deals ⭐')"))

    client = ScriptedClient(
        "SELECT id FROM customers WHERE name = 'Cravings Deals'",
        "That is customer 3.",
    )
    agent = build(database, client, rebind_absent_literals=True)

    result = agent.ask("Which customer is Cravings Deals?")

    assert result.ok
    assert result.result is not None and result.result.row_count == 1
    assert "Cravings Deals ⭐" in result.sql
    assert result.trace.rebound_literals
    assert client.calls == 2  # generate, answer — the repair cost nothing


def test_a_query_that_returns_rows_is_never_rebound(database):
    client = ScriptedClient("SELECT id FROM customers WHERE name = 'Ada'", "Customer 1.")
    agent = build(database, client, rebind_absent_literals=True)

    result = agent.ask("Which customer is Ada?")

    assert result.ok
    assert result.trace.rebound_literals == []


def test_a_rewrite_that_still_returns_nothing_is_discarded(database):
    """Swapping one empty answer for a differently-worded empty answer is not
    a repair. The node has to be monotone or it is a liability."""
    client = ScriptedClient(
        "SELECT id FROM customers WHERE name = 'Nobody At All'", "No customers."
    )
    agent = build(database, client, rebind_absent_literals=True)

    result = agent.ask("Which customer is Nobody At All?")

    assert result.ok
    assert "Nobody At All" in result.sql
    assert result.trace.rebound_literals == []


def test_rebinding_is_off_by_default(database):
    with database.begin() as connection:
        connection.execute(text("INSERT INTO customers VALUES (3, 'Cravings Deals ⭐')"))

    client = ScriptedClient(
        "SELECT id FROM customers WHERE name = 'Cravings Deals'", "No customers."
    )
    agent = build(database, client)

    result = agent.ask("Which customer is Cravings Deals?")

    assert result.result is not None and result.result.row_count == 0
    assert result.trace.rebound_literals == []
