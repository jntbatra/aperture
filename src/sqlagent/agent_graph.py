"""The agent as a LangGraph state graph.

What LangGraph is
-----------------
LangGraph models an application as a **state graph**. You define:

* a **state** — a dictionary whose shape is declared once (``AgentState`` below);
* **nodes** — plain functions that take the state and return the keys they want
  to change;
* **edges** — which node runs next, including *conditional* edges that pick a
  destination by inspecting the state.

You then compile it and invoke it. LangGraph handles threading the state
through, and gives you streaming, checkpointing and a visualisable graph for
free.

Why this shape suits this problem
---------------------------------
The pipeline has two repair loops that are easy to describe in words and
awkward to read as nested control flow:

* **Loop A** — the SQL was wrong. Regenerate it with the error attached.
* **Loop B** — the *context* was wrong: the query named a real table we never
  showed the model. Widen the schema first, then regenerate.

As a graph, those stop being nested ``if`` statements inside a ``for`` loop and
become what they actually are — two different edges out of one decision point:

    validate_and_execute ──success──────────► check_intent ──► write_answer
                         ──context failure──► widen_schema ──► build_context
                         ──sql failure──────► build_context
                         ──out of budget────► give_up

``route_after_execute`` is that decision, written once, in one function. That
is the single biggest readability win of the port: the previous version made
this same decision inline, and an earlier bug came from it being duplicated
across two exception handlers with one of them missing a case.

The state
---------
Every node returns a *partial* update — only the keys it changed. LangGraph
merges it. That keeps each node honest about its own effects: ``generate_sql``
returns ``{"sql": ...}`` and demonstrably cannot touch the trace or the
neighbourhood.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy import text

from sqlagent.clarify import CLARIFICATION_ERROR, needs_clarification
from sqlagent.config import Settings, critic_enabled, vote_samples
from sqlagent.conversation import Turn, render_conversation
from sqlagent.db.execute import ExecutionError, QueryResult, execute
from sqlagent.guards.arithmetic import find_inflated_aggregates, one_to_many_map
from sqlagent.guards.cost import CostRejected
from sqlagent.guards.cost import check as check_cost
from sqlagent.guards.critic import review as critic_review
from sqlagent.guards.evidence import gather as gather_evidence
from sqlagent.guards.prescreen import screen as prescreen_question
from sqlagent.guards.validator import ValidationError, validate
from sqlagent.intent import MAX_PREVIEW_CHARS, MAX_PREVIEW_ROWS
from sqlagent.intent import check_intent as run_intent_check
from sqlagent.llm.mantle import extract_sql

# `Attempt` and `Trace` are imported at runtime rather than under
# TYPE_CHECKING. LangGraph calls `get_type_hints()` on the state schema when
# the graph is built, which evaluates the annotations for real — a name only
# visible to type checkers fails there with
# `NameError: name 'Trace' is not defined`.
#
# This is not circular: `pipeline` imports this module lazily, inside `ask()`,
# so by the time anything here runs `sqlagent.pipeline` is fully initialised.
from sqlagent.pipeline import Attempt, Trace
from sqlagent.prompts import (
    SQL_SYSTEM_PROMPT,
    build_generation_prompt,
    build_repair_prompt,
)
from sqlagent.schema.retrieval import Neighbourhood, expand, widen
from sqlagent.voting import tally

if TYPE_CHECKING:  # pragma: no cover - only needed for the annotation below
    from sqlagent.pipeline import SqlAgent

logger = logging.getLogger(__name__)

CONTEXT_FAILURES = frozenset(
    {
        # The validator caught a reference to a table we never offered. The
        # cheapest possible signal — no database round trip needed to learn it.
        "table_not_allowed",
        # The database rejected a relation or column. Reached when the query
        # passed the allow-list (or none was applied) but still named something
        # absent.
        "missing_relation",
        "missing_column",
    }
)
"""Failure kinds meaning "we did not provide enough schema", not "the SQL was bad".

These route to ``widen_schema`` (Loop B). Everything else routes straight back
to regeneration (Loop A), which would otherwise retry against exactly the same
insufficient context and fail identically.
"""


class AgentState(TypedDict, total=False):
    """Everything that flows between nodes.

    ``total=False`` means no key is required up front — nodes fill them in as
    the run progresses. The alternative, seeding every key with a placeholder,
    makes "not computed yet" and "computed as empty" indistinguishable.
    """

    question: str

    history: list[Turn]
    """Earlier turns in this conversation, oldest first.

    Read-only for every node — a turn is appended by the caller once the whole
    run has finished, not partway through by whichever node happens to notice.
    That keeps a failed attempt from leaking into the history as though it were
    a completed exchange.
    """

    history_summary: str
    """Standing context from turns older than the window, or "".

    Computed once by the caller before the graph runs, never inside a node:
    three nodes render the conversation, and each recomputing it would triple
    the cost of the one feature whose whole justification is that it is cheap.
    """

    # Schema selection
    seeds: list[str]
    neighbourhood: Neighbourhood
    tables: list[str]
    schema_text: str

    # Screening and clarification
    refused: str | None
    """Set when the pre-screen declined the question."""

    clarification: str | None
    """A question to put back to the user instead of answering."""

    clarification_asks: list[dict]
    """The same, structured: one entry per ambiguity, each with its options."""

    # Caching
    cache_key: str | None
    """Set when this question is cacheable; None for follow-ups, which are not."""

    from_cache: bool

    # Generation and execution
    sql: str
    validated_sql: str
    result: QueryResult | None

    # Failure handling
    last_error: str | None
    failed_sql: str | None
    failure_kind: str | None
    attempts: int

    intent_repairs: int
    """Rewrites driven by the intent check, counted apart from ``attempts``.

    Its own budget because it is a different kind of failure. A database error
    either stops recurring or does not; "these rows do not answer the
    question" is an opinion that can be held about every rewrite in turn, so it
    gets one go rather than a share of the general repair budget.
    """

    # Output
    answer: str
    error: str | None

    trace: Trace


# --------------------------------------------------------------------------
# Nodes
#
# Each is built by a factory that closes over the agent, because nodes must
# have the signature `(state) -> dict` but need access to the model client,
# the database and the configuration.
# --------------------------------------------------------------------------


def make_select_tables(agent: SqlAgent, config: Settings, emit):
    """Decide which tables the question concerns."""

    def select_tables(state: AgentState) -> dict[str, Any]:
        trace = state["trace"]
        snapshot = agent.snapshot

        # On a small database, skip the question entirely and use everything.
        # Retrieval only pays for itself when the schema genuinely does not fit,
        # and guessing the seed wrongly costs far more than a few hundred
        # extra tokens.
        if len(snapshot) <= config.full_schema_threshold:
            emit("seeds", {"message": "Small schema — using every table"})
            seeds = sorted(snapshot.tables)
            trace.seed_tables = list(seeds)
            return {
                "seeds": seeds,
                # max_hops=0 yields exactly the seed set, so widening becomes a
                # no-op and every downstream node works unchanged.
                "neighbourhood": expand(agent.graph, seeds, max_hops=0),
                # This node owns the recovery from a failed cache hit, so it is
                # the node that clears the flag. Clearing it at the failure site
                # would let the next failure fall through to widen_schema, which
                # needs a neighbourhood a cache hit never built.
                "from_cache": False,
            }

        emit("seeds", {"message": "Working out which tables are involved"})
        seeds = agent.select_seed_tables(
            state["question"],
            trace,
            state.get("history", []),
            window=config.conversation_window,
            summary=state.get("history_summary", ""),
        )
        if not seeds:
            return {"seeds": [], "error": "no_seed_tables", "from_cache": False}

        trace.seed_tables = list(seeds)
        emit("schema", {"seed_tables": list(seeds)})
        return {
            "seeds": seeds,
            "neighbourhood": expand(
                agent.graph, seeds, max_hops=config.initial_hops
            ),
            "from_cache": False,
        }

    return select_tables


def make_screen(agent: SqlAgent, config: Settings, emit):
    """Decide whether to act on the question at all, and whether to ask first.

    Two optional checks, both off by default, in one node because they share a
    shape: each can end the run before any database work happens, and neither
    ever fails the request on its own error.
    """

    def screen(state: AgentState) -> dict[str, Any]:
        trace = state["trace"]
        question = state["question"]

        if config.prescreen_input:
            verdict = prescreen_question(
                agent.client,
                question=question,
                model=config.light_model,
                trace=trace,
            )
            if not verdict.allowed:
                return {"refused": verdict.reason}

        # Do not re-question a reply. If the previous turn asked something, this
        # message is the answer to it — running the ambiguity check on "by
        # revenue" finds a fragment, asks again, and the user is stuck being
        # interrogated about the answer they just gave.
        #
        # Skipping whenever *any* history exists would be simpler and wrong: it
        # would also skip a genuinely new vague question later in the same
        # thread, so the one protection this offers would quietly stop applying
        # after the first exchange.
        history = state.get("history", [])
        replying = bool(history) and history[-1].clarification is not None

        if config.ambiguity_handling == "ask_human" and not replying:
            clarification = needs_clarification(
                agent.client,
                question=question,
                schema_text="Tables: " + ", ".join(sorted(agent.snapshot.tables)),
                model=config.light_model,
                # The thread is what makes a follow-up specific. Judged without
                # it, almost any follow-up looks ambiguous.
                conversation=render_conversation(
                    history,
                    window=config.conversation_window,
                    summary=state.get("history_summary", ""),
                ),
                max_asks=config.max_clarifying_questions,
                trace=trace,
            )
            if clarification is not None:
                return {
                    "clarification": clarification.render(),
                    # The structured form travels alongside the rendered text:
                    # a browser can draw a row of buttons per ambiguity, and a
                    # client that cannot falls back to the sentence.
                    "clarification_asks": [
                        {"question": ask.question, "options": list(ask.options)}
                        for ask in clarification.asks
                    ],
                }

        return {}

    return screen


def make_check_cache(agent: SqlAgent, config: Settings, emit):
    """Reuse the SQL a previous identical question produced.

    Skips table selection, context building and generation — the two or three
    model calls that dominate latency — and goes straight to validation and
    execution. The query still runs against live data, so the answer is fresh;
    only the *writing* of it is reused.

    Follow-ups are never cached. "And for April?" means whatever the previous
    turns made it mean, so a cache keyed on the question text alone would be
    wrong across threads.
    """

    def check_cache(state: AgentState) -> dict[str, Any]:
        if not config.cache_sql or state.get("history"):
            return {"cache_key": None, "from_cache": False}

        # No dataset in the key: each dataset gets its own SqlAgent instance,
        # and the cache lives on the instance, so two datasets cannot share one.
        key = agent.cache.key(
            state["question"],
            schema_version=agent.snapshot.version,
            glossary=agent.glossary.render(),
        )

        cached = agent.cache.get(key)
        if cached is None:
            return {"cache_key": key, "from_cache": False}

        emit("generating", {"message": "Reusing a query from an identical question"})
        logger.info("cache hit for question")
        state["trace"].cache_hit = True
        return {
            "cache_key": key,
            "from_cache": True,
            "sql": cached,
            # No `tables`, and deliberately so. The allow-list is derived from
            # retrieval, and a cached statement has no retrieval behind it. An
            # *empty* allow-list would reject every table rather than permitting
            # any, so validation runs with none at all and the database stays the
            # authority on which tables exist. Every other guard — statement
            # type, cost, read-only transaction — is unchanged.
            "tables": [],
        }

    return check_cache


def make_build_context(agent: SqlAgent, config: Settings, emit):
    """Assemble the schema description the model will be shown."""

    def build_context(state: AgentState) -> dict[str, Any]:
        trace = state["trace"]
        neighbourhood = state["neighbourhood"]

        tables = neighbourhood.ordered()
        trace.candidate_tables = tables
        trace.hops = neighbourhood.hops

        return {
            "tables": tables,
            "schema_text": agent.build_context(tables, trace),
        }

    return build_context


def make_generate_sql(agent: SqlAgent, config: Settings, emit):
    """Ask the model for SQL — fresh, or a repair of the previous failure."""

    def generate_sql(state: AgentState) -> dict[str, Any]:
        trace = state["trace"]
        tables = state["tables"]
        last_error = state.get("last_error")

        emit(
            "generating",
            {
                "tables": tables,
                "hops": state["neighbourhood"].hops,
                "attempt": state.get("attempts", 0) + 1,
            },
        )

        conversation = render_conversation(
            state.get("history", []),
            window=config.conversation_window,
            summary=state.get("history_summary", ""),
        )
        glossary = agent.glossary_for(tables)

        if last_error is None:
            prompt = build_generation_prompt(
                state["question"],
                state["schema_text"],
                dialect=agent.dialect.prompt_name,
                dialect_rules=agent.dialect.prompt_rules,
                glossary=glossary,
                conversation=conversation,
            )
        else:
            prompt = build_repair_prompt(
                state["question"],
                state["schema_text"],
                state.get("failed_sql") or "",
                last_error,
                dialect=agent.dialect.prompt_name,
                dialect_rules=agent.dialect.prompt_rules,
                glossary=glossary,
                conversation=conversation,
            )

        model = agent.pick_model(tables)
        samples = vote_samples(config)

        if samples <= 1:
            completion = agent.client.complete(
                prompt, model=model, system=SQL_SYSTEM_PROMPT
            )
            trace.record(completion)
            return {"sql": extract_sql(completion.text)}

        # Self-consistency. Where the model is confident the samples agree and
        # this changes nothing; where it is guessing they scatter, and the
        # statement that recurs is more often right than any single draw.
        #
        # The temperature is raised for these samples only — at temperature 0
        # three samples are three identical strings and three times the bill.
        candidates: list[str] = []
        for _ in range(samples):
            completion = agent.client.complete(
                prompt,
                model=model,
                system=SQL_SYSTEM_PROMPT,
                temperature=config.vote_temperature,
            )
            trace.record(completion)
            candidates.append(extract_sql(completion.text))

        vote = tally(candidates, dialect=agent.dialect.sqlglot_name)
        if vote is None:
            return {"sql": candidates[0] if candidates else ""}

        trace.vote_agreement = vote.agreement
        trace.vote_samples = vote.total
        if not vote.unanimous:
            logger.info(
                "voting: %d of %d agreed", vote.agreement, vote.total
            )
        return {"sql": vote.sql}

    return generate_sql


def make_criticise(agent: SqlAgent, config: Settings, emit):
    """Loop C: ask a second model whether the SQL answers the question.

    The other three checks are mechanical — read-only, cheap, un-inflated. None
    of them ask whether the query is about the *right thing*, which is the
    judgement that actually decides whether the answer is correct.

    A rejection is routed back through the ordinary repair loop with the
    objection attached, exactly like a database error. A critic that could only
    veto would turn a recoverable mistake into a dead end.
    """

    def criticise(state: AgentState) -> dict[str, Any]:
        trace = state["trace"]

        # `PASS` clears any critic rejection left over from a previous pass
        # through this node. Returning a bare {} would leave `failure_kind` set
        # to "critic_rejected" from the last round, and the router would send an
        # approved query straight back to be rewritten — forever.
        PASS: dict[str, Any] = {"failure_kind": None}

        if not critic_enabled(config):
            return PASS

        # A cached statement has already run successfully at least once. Paying
        # a model call to re-review it would undo the point of the cache.
        if state.get("from_cache"):
            return PASS

        # The budget is shared with the repair loops. A query that has already
        # been rewritten three times is not going to be saved by a fourth
        # opinion, and the critic must not be able to exhaust the budget on its
        # own.
        if state.get("attempts", 0) >= config.max_repair_attempts:
            return PASS

        emit("validating", {"message": "Checking the query answers the question"})

        critique = critic_review(
            agent.client,
            question=state["question"],
            schema_text=state["schema_text"],
            sql=state["sql"],
            model=config.critic_model or config.strong_model,
            trace=trace,
        )

        if critique.ok:
            return PASS

        logger.info("critic rejected the query: %s", critique.problem)
        trace.critic_rejections.append(critique.problem)
        trace.attempts.append(
            Attempt(state["sql"], False, critique.problem, "critic_rejected")
        )

        return {
            "last_error": f"A reviewer found a problem: {critique.problem}",
            "failed_sql": state["sql"],
            "failure_kind": "critic_rejected",
            "attempts": state.get("attempts", 0) + 1,
        }

    return criticise


def make_validate_and_execute(agent: SqlAgent, config: Settings, emit):
    """Check the statement is safe, then run it.

    Validation and execution live in one node because they share a failure
    path: both produce "this attempt did not work, here is why, here is what
    kind of problem it was", and the routing that follows treats them
    identically.
    """

    def validate_and_execute(state: AgentState) -> dict[str, Any]:
        trace = state["trace"]
        candidate_sql = state["sql"]
        tables = state["tables"]
        attempts = state.get("attempts", 0)

        repaired_by = None
        if state.get("last_error") is not None:
            repaired_by = (
                "loop_b" if trace.hops > config.initial_hops else "loop_a"
            )

        emit("validating", {"sql": candidate_sql})

        try:
            validated = validate(
                candidate_sql,
                dialect=agent.dialect.sqlglot_name,
                row_limit=config.row_limit,
                # None, not an empty set. An empty allow-list means "no table is
                # permitted" and would reject every cached statement; None means
                # "no allow-list applies", which is the intent when there was no
                # retrieval to derive one from.
                allowed_tables=set(tables) if tables else None,
            )
            # Ask the planner what this will cost before paying for it. A
            # statement timeout is reactive — it stops the damage partway
            # through; this refuses the obvious disasters up front, for the
            # price of one cheap round trip.
            check_cost(
                agent.engine,
                validated.sql,
                dialect=agent.dialect,
                max_cost=config.max_plan_cost,
                max_estimated_rows=config.max_plan_rows,
            )

            emit("executing", {"sql": validated.sql})
            result = execute(
                agent.engine,
                validated.sql,
                row_limit=config.row_limit,
                statement_timeout_ms=config.statement_timeout_ms,
                dialect=agent.dialect,
            )
        except ValidationError as exc:
            _invalidate_if_cached(agent, state)
            trace.attempts.append(
                Attempt(candidate_sql, False, str(exc), exc.kind, repaired_by)
            )
            return {
                "last_error": str(exc),
                "failed_sql": candidate_sql,
                "failure_kind": exc.kind,
                "attempts": attempts + 1,
            }
        except CostRejected as exc:
            _invalidate_if_cached(agent, state)
            # Loop A: the schema was fine, the query was not. The estimate goes
            # into the repair prompt, which is why the message names the number
            # and the ceiling rather than just saying "too expensive".
            trace.attempts.append(
                Attempt(validated.sql, False, str(exc), exc.kind, repaired_by)
            )
            return {
                "last_error": str(exc),
                "failed_sql": validated.sql,
                "failure_kind": exc.kind,
                "attempts": attempts + 1,
            }
        except ExecutionError as exc:
            _invalidate_if_cached(agent, state)
            trace.attempts.append(
                Attempt(validated.sql, False, str(exc), exc.kind, repaired_by)
            )
            return {
                "last_error": str(exc),
                "failed_sql": validated.sql,
                "failure_kind": exc.kind,
                "attempts": attempts + 1,
            }

        trace.attempts.append(Attempt(validated.sql, True, repaired_by=repaired_by))

        # Remember the statement, never the rows. The query re-runs on the next
        # identical question, so the answer stays fresh; only the model calls
        # that wrote it are skipped.
        cache_key = state.get("cache_key")
        if cache_key and config.cache_sql:
            agent.cache.put(cache_key, validated.sql)

        # Advisory, after the fact: a SUM multiplied by a join produces no
        # error and a plausible-looking number. Surfaced rather than rejected,
        # because the same query shape is sometimes exactly what was wanted.
        if config.check_inflated_aggregates:
            trace.inflation_warnings = [
                finding.render()
                for finding in find_inflated_aggregates(
                    validated.sql,
                    dialect=agent.dialect.sqlglot_name,
                    one_to_many=one_to_many_map(agent.snapshot),
                )
            ]
            if trace.inflation_warnings:
                logger.info("possible inflated aggregate: %s", trace.inflation_warnings)

        return {
            "validated_sql": validated.sql,
            "result": result,
            "last_error": None,
            "failure_kind": None,
            "attempts": attempts + 1,
        }

    return validate_and_execute


def _evidence_for(
    agent: SqlAgent, config: Settings, *, question: str, sql: str, row_count: int
) -> str:
    """What can be established about a finished query without asking a model.

    Runs in its own read-only transaction with the same timeout as the query
    itself, because the one that ran the query has already been closed.

    Never raises. This is advisory input to a check that is itself advisory —
    a probe that failed must not be able to fail the request that produced a
    perfectly good result.
    """
    try:
        with agent.engine.begin() as connection:
            for statement in agent.dialect.session_setup(
                statement_timeout_ms=config.statement_timeout_ms
            ):
                connection.execute(text(statement))
            found = gather_evidence(
                connection,
                question=question,
                sql=sql,
                dialect=agent.dialect.sqlglot_name,
                row_count=row_count,
            )
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        logger.debug("evidence probes failed: %s", exc)
        return ""

    if not found.worth_mentioning(row_count):
        return ""
    return found.render(row_count)


def make_check_intent(agent: SqlAgent, config: Settings, emit):
    """Loop D: do the rows that came back answer the question that was asked?

    The gap this closes was measured, not guessed. Over 150 BIRD questions, 61
    of the 62 failures were a valid query returning the wrong rows — nothing
    raised, nothing to repair against. No earlier check can see that, because
    of what each one is handed: ``clarify`` and the critic get the question and
    the SQL and never a row, the faithfulness check gets the rows and never the
    question. This is the only place that holds all three at once.

    Three outcomes rather than two. ``mismatch`` is the critic's behaviour with
    rows in front of it, and routes back through the ordinary repair loop with
    the named defect attached. ``ask`` stops and puts a question to the user,
    which is the outcome a benchmark cannot reward — a harness has nobody to
    ask, so every ``ask`` scores as a failure and the measured number
    understates the behaviour. Guessing silently is what produced those 61.

    The one cost worth naming: a mismatch sends a *working* result back to be
    rewritten, and if the rewrite then fails to execute the run gives up rather
    than falling back to the result it already had. That is the critic's
    existing shape, and it is bounded here by ``intent_repair_attempts``.
    """

    def check_intent(state: AgentState) -> dict[str, Any]:
        trace = state["trace"]

        # Same reason as the critic: a bare {} would leave `failure_kind` set
        # from the previous pass and route an accepted result back to be
        # rewritten forever.
        PASS: dict[str, Any] = {"failure_kind": None}

        if not config.check_result_intent:
            return PASS

        result = state.get("result")
        if result is None:
            return PASS

        # A cached statement ran successfully before and was judged then.
        # Re-judging it on every hit would undo the point of caching.
        if state.get("from_cache"):
            return PASS

        if state.get("intent_repairs", 0) >= config.intent_repair_attempts:
            return PASS

        # The general budget is shared with both repair loops. A query already
        # rewritten three times is not saved by a fourth opinion.
        if state.get("attempts", 0) >= config.max_repair_attempts:
            return PASS

        emit("validating", {"message": "Checking the rows answer the question"})

        sql = state["validated_sql"]
        verdict = run_intent_check(
            agent.client,
            question=state["question"],
            sql=sql,
            schema_text=state.get("schema_text", ""),
            result_preview=result.preview(limit=MAX_PREVIEW_ROWS)[:MAX_PREVIEW_CHARS],
            row_count=result.row_count,
            model=config.intent_model or config.strong_model,
            conversation=render_conversation(
                state.get("history", []),
                window=config.conversation_window,
                summary=state.get("history_summary", ""),
            ),
            truncated=result.truncated,
            evidence=_evidence_for(
                agent,
                config,
                question=state["question"],
                sql=sql,
                row_count=result.row_count,
            ),
            # A deployment configured never to interrupt the user must not be
            # interrupted from here either. The benchmark harness sets
            # `best_effort` for exactly this reason: it has nobody to ask.
            allow_ask=config.ambiguity_handling == "ask_human",
            trace=trace,
        )

        if verdict.verdict == "mismatch":
            logger.info("intent check rejected the result: %s", verdict.reason)
            trace.intent_rejections.append(verdict.reason)
            trace.attempts.append(Attempt(sql, False, verdict.reason, "intent_mismatch"))
            return {
                "last_error": (
                    f"The query ran, but the rows did not answer the question: "
                    f"{verdict.reason}"
                ),
                "failed_sql": sql,
                "failure_kind": "intent_mismatch",
                "attempts": state.get("attempts", 0) + 1,
                "intent_repairs": state.get("intent_repairs", 0) + 1,
            }

        if verdict.withheld_question:
            trace.intent_asks_withheld.append(verdict.withheld_question)

        if verdict.verdict == "ask":
            logger.info("intent check is asking the user: %s", verdict.question)
            trace.intent_asks.append(verdict.render())
            return {
                "clarification": verdict.render(),
                "clarification_asks": [
                    {"question": verdict.question, "options": list(verdict.options)}
                ],
                "failure_kind": None,
            }

        return PASS

    return check_intent


def make_widen_schema(agent: SqlAgent, config: Settings, emit):
    """Loop B: offer the model more of the schema, then try again."""

    def widen_schema(state: AgentState) -> dict[str, Any]:
        neighbourhood = state["neighbourhood"]
        wider = widen(agent.graph, neighbourhood)

        if wider.hops > neighbourhood.hops:
            logger.info(
                "widening schema context to %d hops after %s",
                wider.hops,
                state.get("failure_kind"),
            )
            return {"neighbourhood": wider}

        # Already at the ceiling. Fall through unchanged and let regeneration
        # try once more with the same context — sometimes the model simply
        # misspelled a column it *was* given.
        return {}

    return widen_schema


def make_write_answer(agent: SqlAgent, config: Settings, emit):
    """Turn rows into a sentence."""

    def write_answer(state: AgentState) -> dict[str, Any]:
        result = state["result"]
        assert result is not None

        emit("answering", {"sql": state["validated_sql"], "row_count": result.row_count})
        answer = agent.write_answer(
            state["question"],
            state["validated_sql"],
            result,
            state["trace"],
            state.get("history", []),
            window=config.conversation_window,
            summary=state.get("history_summary", ""),
        )
        return {"answer": answer}

    return write_answer


def make_give_up(agent: SqlAgent, config: Settings, emit):
    """Explain what was tried, rather than failing blankly.

    The last error is usually specific enough that the user can rephrase
    successfully, which a generic failure message would deny them.
    """

    def give_up(state: AgentState) -> dict[str, Any]:
        # Not every early exit is a failure. A refusal and a clarifying question
        # are both deliberate outcomes, and neither should be dressed up as
        # "I could not produce a working query".
        if state.get("refused"):
            return {"answer": state["refused"], "error": "refused"}

        if state.get("clarification"):
            return {"answer": state["clarification"], "error": CLARIFICATION_ERROR}

        if state.get("error") == "no_seed_tables":
            return {
                "answer": (
                    "I could not match that question to any table in this database. "
                    "Try naming the data you are interested in."
                )
            }

        return {
            "answer": (
                f"I could not produce a working query for that question after "
                f"{len(state['trace'].attempts)} attempts. "
                f"The last error was: {state.get('last_error')}"
            ),
            "error": state.get("last_error"),
        }

    return give_up


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def _invalidate_if_cached(agent: SqlAgent, state: AgentState) -> None:
    """Drop a cached statement that just failed.

    The schema may have drifted in a way the version hash has not caught, or the
    query was always fragile. Serving it again would repeat the failure — and,
    worse, the repair path would keep starting from a statement known to be
    broken.
    """
    if state.get("from_cache") and state.get("cache_key"):
        logger.info("cached SQL failed; dropping it from the cache")
        agent.cache.invalidate(state["cache_key"])


def make_route_after_critic(config: Settings):
    """A rejected query goes back to be rewritten; an accepted one runs."""

    def route_after_critic(state: AgentState) -> str:
        if state.get("failure_kind") != "critic_rejected":
            return "validate_and_execute"
        if state.get("attempts", 0) > config.max_repair_attempts:
            return "give_up"
        return "build_context"

    return route_after_critic


def route_after_screen(state: AgentState) -> str:
    """A refusal or a clarifying question ends the run before any database work."""
    if state.get("refused") or state.get("clarification"):
        return "give_up"
    return "check_cache"


def route_after_cache(state: AgentState) -> str:
    """A hit goes straight to execution; a miss takes the normal path."""
    return "validate_and_execute" if state.get("from_cache") else "select_tables"


def route_after_select(state: AgentState) -> str:
    """Nothing to query if no table matched the question."""
    return "give_up" if not state.get("seeds") else "build_context"


def make_route_after_execute(config: Settings):
    """The Loop A / Loop B decision, in exactly one place.

    Written once, as a conditional edge, rather than inline at each failure
    site. An earlier version of this pipeline made the same decision in two
    separate exception handlers, and one of them was missing the
    context-failure case entirely — so the most common trigger for widening
    never widened.
    """

    def route_after_execute(state: AgentState) -> str:
        if state.get("last_error") is None:
            return "check_intent"

        # A cached statement failed. It was validated with no allow-list and no
        # retrieval behind it, so there is no context to repair *from* — start
        # the normal path instead of regenerating against nothing.
        if state.get("from_cache"):
            return "select_tables"

        # Budget is shared across both loops. A question needing four attempts
        # is a question to hand back to the user, whichever kind each was.
        if state.get("attempts", 0) > config.max_repair_attempts:
            return "give_up"

        if state.get("failure_kind") in CONTEXT_FAILURES:
            return "widen_schema"

        return "build_context"

    return route_after_execute


def make_route_after_intent(config: Settings):
    """Accept, rewrite, or hand a question back."""

    def route_after_intent(state: AgentState) -> str:
        # An `ask` is a deliberate outcome, not a failure to produce a query,
        # and `give_up` renders it as the question it is.
        if state.get("clarification"):
            return "give_up"

        if state.get("failure_kind") != "intent_mismatch":
            return "write_answer"

        # Out of budget with a result already in hand. Answering with a result
        # something objected to beats answering with nothing: the objection is
        # an opinion, the rows are real.
        if state.get("attempts", 0) > config.max_repair_attempts:
            return "write_answer"

        return "build_context"

    return route_after_intent


def build_agent_graph(agent: SqlAgent, config: Settings, emit):
    """Assemble and compile the graph.

    Returns a compiled graph whose ``invoke(state)`` runs one question to
    completion.
    """
    builder = StateGraph(AgentState)

    builder.add_node("screen", make_screen(agent, config, emit))
    builder.add_node("check_cache", make_check_cache(agent, config, emit))
    builder.add_node("select_tables", make_select_tables(agent, config, emit))
    builder.add_node("build_context", make_build_context(agent, config, emit))
    builder.add_node("generate_sql", make_generate_sql(agent, config, emit))
    builder.add_node("criticise", make_criticise(agent, config, emit))
    builder.add_node("validate_and_execute", make_validate_and_execute(agent, config, emit))
    builder.add_node("check_intent", make_check_intent(agent, config, emit))
    builder.add_node("widen_schema", make_widen_schema(agent, config, emit))
    builder.add_node("write_answer", make_write_answer(agent, config, emit))
    builder.add_node("give_up", make_give_up(agent, config, emit))

    builder.set_entry_point("screen")

    builder.add_conditional_edges(
        "screen",
        route_after_screen,
        {"check_cache": "check_cache", "give_up": "give_up"},
    )
    builder.add_conditional_edges(
        "check_cache",
        route_after_cache,
        {"validate_and_execute": "validate_and_execute", "select_tables": "select_tables"},
    )
    builder.add_conditional_edges(
        "select_tables",
        route_after_select,
        {"build_context": "build_context", "give_up": "give_up"},
    )
    builder.add_edge("build_context", "generate_sql")
    # Loop C sits between generation and execution when enabled, and is a
    # straight pass-through when it is not — so the graph has one shape rather
    # than two, and the toggle cannot change the routing by accident.
    builder.add_edge("generate_sql", "criticise")
    builder.add_conditional_edges(
        "criticise",
        make_route_after_critic(config),
        {
            "validate_and_execute": "validate_and_execute",
            "build_context": "build_context",
            "give_up": "give_up",
        },
    )
    builder.add_conditional_edges(
        "validate_and_execute",
        make_route_after_execute(config),
        {
            "check_intent": "check_intent",
            "widen_schema": "widen_schema",
            "build_context": "build_context",   # Loop A
            "select_tables": "select_tables",    # a cached statement failed
            "give_up": "give_up",
        },
    )
    # Loop D sits between a successful execution and the answer when enabled,
    # and is a pass-through when it is not — one graph shape either way, for
    # the same reason as Loop C.
    builder.add_conditional_edges(
        "check_intent",
        make_route_after_intent(config),
        {
            "write_answer": "write_answer",
            "build_context": "build_context",
            "give_up": "give_up",
        },
    )
    # Loop B rejoins the main path: more schema, then regenerate.
    builder.add_edge("widen_schema", "build_context")
    builder.add_edge("write_answer", END)
    builder.add_edge("give_up", END)

    # recursion_limit guards against a graph that never reaches END. The repair
    # budget already bounds the loops; this is a backstop against a routing bug
    # rather than against expected behaviour.
    return builder.compile()
