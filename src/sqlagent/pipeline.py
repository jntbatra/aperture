"""The agent: question in, answer out.

The orchestration lives in :mod:`sqlagent.agent_graph` as a LangGraph state
graph. This module holds the public surface — ``SqlAgent.ask()`` — plus the
result and trace types, and the individual steps the graph's nodes call.

The shape of a request
----------------------

    select_tables ──► build_context ──► generate_sql ──► validate_and_execute
         │                  ▲                                    │
         │                  │                                    │
         │                  ├──────────── Loop A ────────────────┤
         │                  │        (SQL was wrong)             │
         │                  │                                    │
         │            widen_schema ◄───── Loop B ────────────────┤
         │           (context was too narrow)                    │
         │                                                       │
         └──► give_up ◄──── out of budget ───────────────────────┤
                                                                 │
                                        write_answer ◄──success──┘

Why a graph
-----------
The two repair loops are the whole difficulty of this pipeline, and they are
genuinely different:

* **Loop A** — the SQL was wrong. Same context, regenerate with the error.
* **Loop B** — the context was wrong: the query named a real table we never
  showed the model. Widening must happen *before* regenerating, or the retry
  fails identically.

Expressed as nested control flow, that decision tends to get duplicated at each
failure site — and an earlier version of this code did exactly that, with one
copy missing the context-failure case, so the most common trigger for widening
never widened. As a graph it is one conditional edge, written once.

LangGraph also gives streaming and checkpointing for free, which matters for the
progress events the web UI consumes and for the pause-and-resume work on the
roadmap.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import networkx as nx
from sqlalchemy import Engine

from sqlagent.cache import SqlCache
from sqlagent.config import Settings, apply_overrides, options_of, settings
from sqlagent.conversation import (
    Turn,
    carryable_result,
    evicted_turns,
    mentioned_tables,
    render_conversation,
)
from sqlagent.db.dialects import Dialect, for_engine_name
from sqlagent.db.execute import QueryResult
from sqlagent.db.profile import profile_tables
from sqlagent.db.sample import sample_tables
from sqlagent.glossary import Glossary
from sqlagent.guards import faithfulness
from sqlagent.llm.mantle import Completion, MantleClient, json_from_reply
from sqlagent.prompts import ANSWER_SYSTEM_PROMPT, build_answer_prompt, render_schema
from sqlagent.report import (
    SYNTHESIS_SYSTEM_PROMPT,
    build_synthesis_prompt,
    decompose,
)
from sqlagent.schema.graph import build_graph
from sqlagent.schema.introspect import SchemaSnapshot, reflect_schema
from sqlagent.summarise import SummaryCache, summarise

logger = logging.getLogger(__name__)

ProgressHook = Callable[[str, dict], None]
"""Called as the run moves between nodes: ``hook(stage, detail)``.

Exists so a web UI can show what the agent is doing while it works — the
alternative is a spinner for several seconds with no explanation. Stages are
``seeds``, ``schema``, ``generating``, ``validating``, ``executing`` and
``answering``.

Failures inside a hook are swallowed: progress reporting must never be able to
break a request that would otherwise have succeeded.
"""


@dataclass
class Attempt:
    """One pass through generate → validate → execute."""

    sql: str
    ok: bool
    error: str | None = None
    error_kind: str | None = None
    repaired_by: str | None = None
    """Which loop produced this attempt: ``"loop_a"``, ``"loop_b"``, or None
    for the first try."""


@dataclass
class Trace:
    """Everything that happened, for debugging, metrics and evaluation.

    One object per request rather than scattered log lines: a wrong answer can
    be diagnosed by reading a single record, and metrics (repair rate, token
    cost, latency) are all derivable from it without separate counters.
    """

    question: str
    seed_tables: list[str] = field(default_factory=list)
    candidate_tables: list[str] = field(default_factory=list)
    hops: int = 0
    sampled: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    model_calls: int = 0

    inflation_warnings: list[str] = field(default_factory=list)
    """Aggregates that a join in the same query may have multiplied.

    Advisory. The query succeeded and the number is reported, but a figure that
    may be inflated is worth saying so about — the alternative is a plausible
    wrong number with nothing to distinguish it from a right one."""

    parts: list[str] = field(default_factory=list)
    """Sub-questions, when the question was answered in parts. Empty otherwise."""

    vote_agreement: int = 0
    """How many sampled candidates agreed on the statement that ran.

    0 when voting was off. Low agreement is the useful signal: it means the
    model was guessing, which is worth surfacing even when the query worked."""

    vote_samples: int = 0

    critic_rejections: list[str] = field(default_factory=list)
    """Defects the critic named, in order. Each one triggered a regeneration."""

    cache_hit: bool = False
    """The SQL came from the cache; generation was skipped entirely."""

    answer_corrected: bool = False
    """The first answer said something the results did not support, and was
    regenerated. Worth counting: a rising rate means the answer model is drifting
    from the rows, which no SQL-level metric would show."""

    answer_unverified: bool = False
    """Still unsupported after one correction. The answer is returned anyway —
    refusing to answer is worse — but it is flagged rather than passed off as
    checked."""

    @property
    def repair_count(self) -> int:
        """How many retries were needed. Zero means first-attempt success."""
        return max(0, len(self.attempts) - 1)

    def record(self, completion: Completion) -> None:
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        self.model_calls += 1


@dataclass
class AgentResult:
    """What the caller gets back."""

    question: str
    answer: str
    sql: str | None
    result: QueryResult | None
    trace: Trace
    error: str | None = None

    clarification_asks: list[dict] = field(default_factory=list)
    """One entry per ambiguity, each with its options, when the agent asked
    rather than answered. Empty otherwise."""

    @property
    def ok(self) -> bool:
        return self.error is None


class SqlAgent:
    """Answers natural-language questions about one database.

    Construct once and reuse. The expensive setup — reflecting the schema and
    building the table graph — happens once in :meth:`warm`, not per question.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        client: MantleClient | None = None,
        config: Settings | None = None,
    ) -> None:
        self.engine = engine
        self.config = config or settings()
        # Dialect decides how sessions are hardened, how errors are read, and
        # what SQL flavour the prompts and validator expect.
        self.dialect: Dialect = for_engine_name(engine.dialect.name)
        self.client = client or MantleClient(self.config)

        self._snapshot: SchemaSnapshot | None = None
        self._graph: nx.DiGraph | None = None

        # Domain facts the schema cannot carry — units, ambiguous terms, named
        # metrics. Loaded once; an empty glossary renders to nothing, so a
        # database with no declarations produces exactly the prompts it did
        # before glossaries existed.
        # Question -> SQL. Bounded, per-agent, and holding no result data;
        # see `sqlagent.cache` for why the rows are deliberately not kept.
        self.cache = SqlCache(max_entries=self.config.cache_max_entries)

        # Standing context distilled from turns that have fallen out of the
        # window. In process and bounded: a summary is derived data, and losing
        # it costs one model call, so it does not belong in the store.
        self._summaries = SummaryCache()

        self.glossary = Glossary.load(self.config.glossary_path)
        if self.glossary:
            logger.info(
                "glossary loaded: %d column notes, %d terms, %d metrics",
                len(self.glossary.columns),
                len(self.glossary.terms),
                len(self.glossary.metrics),
            )

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def warm(self) -> None:
        """Reflect the schema and build the table graph.

        Called automatically on first use. Exposed separately so a server can
        pay this cost at startup rather than on a user's first question.
        """
        self._snapshot = reflect_schema(self.engine)
        self._graph = build_graph(self._snapshot)
        logger.info(
            "schema loaded: %d tables, version %s",
            len(self._snapshot),
            self._snapshot.version,
        )

    @property
    def snapshot(self) -> SchemaSnapshot:
        if self._snapshot is None:
            self.warm()
        assert self._snapshot is not None
        return self._snapshot

    @property
    def graph(self) -> nx.DiGraph:
        """The **schema** graph: tables as nodes, foreign keys as edges.

        Not to be confused with the LangGraph state graph that orchestrates a
        request; that one is built in :mod:`sqlagent.agent_graph`.
        """
        if self._graph is None:
            self.warm()
        assert self._graph is not None
        return self._graph

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def ask(
        self,
        question: str,
        *,
        history: Sequence[Turn] = (),
        options: dict | None = None,
        on_progress: ProgressHook | None = None,
    ) -> AgentResult:
        """Answer a question. Never raises; failures come back in the result.

        A question that cannot be answered is a normal outcome, not an
        exception — the callers are a web handler and a benchmark runner, and
        both want a structured result either way.

        Args:
            question: What was asked, verbatim. May be a fragment that only
                means something against ``history`` ("and for April?").
            history: Earlier turns in this conversation, oldest first. Empty for
                a standalone question, which is what the benchmark runner and
                the CLI pass — so their behaviour is unchanged by this argument
                existing.
            options: Per-request overrides for the quality toggles — "be more
                careful with this one" is a decision made while asking, not
                while configuring a server. Restricted to
                ``config.OVERRIDABLE``; anything else is ignored.
        """
        # Imported here rather than at module scope: agent_graph imports the
        # types defined above, so a top-level import would be circular.
        from sqlagent.agent_graph import build_agent_graph

        started = time.monotonic()
        trace = Trace(question=question)
        emit = _safe_hook(on_progress)

        # Settings for this question alone. Identical to `self.config` when
        # nothing was overridden, so the ordinary path is unchanged.
        config = apply_overrides(self.config, options)

        # A question that is really several questions takes a different path:
        # each part is answered through this same method, then the findings are
        # written up as one answer. Only attempted for a first question — a
        # follow-up refines something, it does not open a new investigation.
        if config.decompose_questions and not history:
            # Screening runs *first*. Decomposition happens above the graph, so
            # it used to skip the ambiguity check entirely: a vague question was
            # split into vague parts, each answered badly, and the findings
            # synthesised into content-free prose ("exact numerical data would
            # need to be referenced from the detailed findings"). Splitting a
            # question nobody has pinned down multiplies the guessing rather
            # than removing it.
            stopped = self._screen_before_answering(question, trace, started, config)
            if stopped is not None:
                return stopped

            multi = self._answer_in_parts(question, trace, emit, started, config)
            if multi is not None:
                return multi

        try:
            # Compiled per request because the progress hook differs per call.
            # Compilation is microseconds against several seconds of model
            # calls, so caching it would optimise the wrong thing.
            graph = build_agent_graph(self, config, emit)

            final = graph.invoke(
                {
                    "question": question,
                    "history": list(history),
                    # Computed once, here, rather than inside a node: three
                    # nodes render the conversation and each would otherwise
                    # summarise it again, tripling the cost of a feature whose
                    # entire point is to be cheap.
                    "history_summary": self.summarise_history(history, config, trace),
                    "trace": trace,
                    "attempts": 0,
                },
                # Backstop against a routing bug producing an endless walk. The
                # repair budget already bounds normal looping.
                {"recursion_limit": 50},
            )
        except Exception as exc:  # noqa: BLE001 - deliberate top-level barrier
            logger.exception("unhandled failure answering question")
            trace.seconds = round(time.monotonic() - started, 3)
            return AgentResult(
                question=question,
                answer="Something went wrong while answering that question.",
                sql=None,
                result=None,
                trace=trace,
                error=str(exc),
            )

        trace.seconds = round(time.monotonic() - started, 3)

        return AgentResult(
            question=question,
            answer=final.get("answer", ""),
            sql=final.get("validated_sql") or final.get("failed_sql"),
            result=final.get("result"),
            trace=trace,
            error=final.get("error"),
            clarification_asks=final.get("clarification_asks", []),
        )

    def summarise_history(
        self, history: Sequence[Turn], config: Settings, trace: Trace
    ) -> str:
        """Standing context from the turns this question will not see in full.

        Returns an empty string whenever there is nothing beyond the window,
        which is every first question, every benchmark question and every CLI
        invocation — so none of them pay for this and none of their numbers
        change.

        Incremental: the cache is asked for the longest prefix of the evicted
        turns it already knows, and only the turns after that are folded in.
        One question therefore costs at most one summarisation call, not one
        per turn of the conversation so far.
        """
        if not config.summarise_conversation:
            return ""

        evicted = evicted_turns(history, window=config.conversation_window)
        if not evicted:
            return ""

        cached = self._summaries.get(evicted)
        if cached is not None:
            return cached

        previous, pending = self._summaries.nearest(evicted)
        summary = summarise(
            self.client,
            evicted=pending,
            previous=previous,
            model=config.light_model,
            trace=trace,
        )
        self._summaries.put(evicted, summary)
        return summary

    def _screen_before_answering(
        self, question: str, trace: Trace, started: float, config: Settings
    ) -> AgentResult | None:
        """Refuse or ask, before any work happens. None means carry on.

        Duplicates what the graph's ``screen`` node does, for the one path that
        does not reach the graph. It is only reached when decomposition is on —
        every other question is screened inside the graph exactly once, so this
        cannot double the model calls on the ordinary path.
        """
        from sqlagent.clarify import CLARIFICATION_ERROR, needs_clarification
        from sqlagent.guards.prescreen import screen as prescreen_question

        def stop(answer: str, error: str, asks: list[dict] | None = None) -> AgentResult:
            trace.seconds = round(time.monotonic() - started, 3)
            return AgentResult(
                question=question, answer=answer, sql=None, result=None,
                trace=trace, error=error, clarification_asks=asks or [],
            )

        if config.prescreen_input:
            verdict = prescreen_question(
                self.client, question=question, model=config.light_model, trace=trace
            )
            if not verdict.allowed:
                return stop(verdict.reason, "refused")

        if config.ambiguity_handling == "ask_human":
            clarification = needs_clarification(
                self.client,
                question=question,
                schema_text="Tables: " + ", ".join(sorted(self.snapshot.tables)),
                model=config.light_model,
                trace=trace,
            )
            if clarification is not None:
                return stop(
                    clarification.render(),
                    CLARIFICATION_ERROR,
                    [
                        {"question": ask.question, "options": list(ask.options)}
                        for ask in clarification.asks
                    ],
                )

        return None

    def _answer_in_parts(
        self,
        question: str,
        trace: Trace,
        emit: ProgressHook,
        started: float,
        config: Settings,
    ) -> AgentResult | None:
        """Answer a multi-part question, or return None if it is not one.

        Each part runs through ``ask()`` unchanged — same retrieval, same
        validator, same cost gate. A sub-question is just a question, and giving
        it its own reduced pipeline would mean two code paths with two sets of
        guarantees.
        """
        parts = decompose(
            self.client,
            question=question,
            schema_text="Tables: " + ", ".join(sorted(self.snapshot.tables)),
            model=config.light_model,
            trace=trace,
        )
        if not parts:
            return None

        logger.info("answering in %d parts", len(parts))
        emit("seeds", {"message": f"That needs {len(parts)} queries — working through them"})

        findings: list[tuple[str, str]] = []
        last_with_sql: AgentResult | None = None

        # Earlier parts become context for later ones. They have to: "get the
        # latest order" followed by "details of the customer who placed that
        # order" is two parts where the second cannot be answered alone.
        #
        # Run without this, the second part had no referent for "that order" and
        # the model wrote `WHERE id = '0c5eec76-...'` — a real id, belonging to
        # the *oldest* order in the database, cancelled in May. The answer then
        # spliced correct order details onto the wrong customer.
        #
        # The conversation machinery already handles exactly this: it carries
        # small results forward and instructs the model never to write an id
        # that does not appear in them. Reusing it here rather than inventing a
        # second, weaker mechanism.
        done: list[Turn] = []

        for index, part in enumerate(parts, start=1):
            emit("generating", {"message": f"Part {index} of {len(parts)}: {part}"})
            # Two settings are forced off for the parts. `decompose_questions`,
            # or a part the model considers compound would decompose again and
            # again. And `ambiguity_handling`, because the parts are machine-
            # generated from a question the user has already been asked about —
            # interrogating them about wording they never wrote is absurd, and
            # would stall the run four times over.
            answer = self.ask(
                part,
                history=done,
                options={
                    **options_of(config),
                    "decompose_questions": False,
                    "ambiguity_handling": "best_effort",
                },
            )

            columns, carried = (
                carryable_result(answer.result.columns, answer.result.rows)
                if answer.result is not None
                else ((), ())
            )
            done.append(
                Turn(
                    question=part,
                    sql=answer.sql,
                    ok=answer.ok,
                    result_columns=columns,
                    result_rows=carried,
                    row_count=answer.result.row_count if answer.result else None,
                )
            )
            findings.append((part, answer.answer))
            trace.input_tokens += answer.trace.input_tokens
            trace.output_tokens += answer.trace.output_tokens
            trace.model_calls += answer.trace.model_calls
            trace.attempts.extend(answer.trace.attempts)
            if answer.sql:
                last_with_sql = answer

        emit("answering", {"message": "Putting the findings together"})
        completion = self.client.complete(
            build_synthesis_prompt(question, findings),
            model=config.strong_model,
            system=SYNTHESIS_SYSTEM_PROMPT,
        )
        trace.record(completion)

        trace.parts = [part for part, _ in findings]
        trace.candidate_tables = (
            last_with_sql.trace.candidate_tables if last_with_sql else []
        )
        trace.seconds = round(time.monotonic() - started, 3)

        return AgentResult(
            question=question,
            answer=completion.text,
            # The last part's SQL, with every part's query in the trace. One
            # statement cannot represent several, and claiming otherwise would
            # show the user a query that did not produce most of the answer.
            sql=last_with_sql.sql if last_with_sql else None,
            result=last_with_sql.result if last_with_sql else None,
            trace=trace,
        )

    # ------------------------------------------------------------------
    # Steps — called by the graph's nodes
    # ------------------------------------------------------------------

    def select_seed_tables(
        self,
        question: str,
        trace: Trace,
        history: Sequence[Turn] = (),
        *,
        window: int | None = None,
        summary: str = "",
    ) -> list[str]:
        """Ask which tables the question concerns.

        Only table *names* are sent, not columns — for a 56-table schema that is
        a few hundred tokens rather than several thousand. The graph walk then
        fills in whatever these tables join to, so this only has to be roughly
        right, not complete.

        Falls back to literal name matching if the model returns nothing
        usable. A cheap deterministic fallback keeps the agent working when the
        model is having a bad day, and makes the unit tests independent of
        model behaviour.

        On a follow-up the fallback matters far more than usual: "break that
        down by city" contains no table name at all, so name matching finds
        nothing. The previous turns' SQL is used instead — it names the tables
        outright, which is a better signal than anything in the question text.
        """
        table_names = sorted(self.snapshot.tables)
        conversation = render_conversation(
            list(history),
            window=self.config.conversation_window if window is None else window,
            summary=summary,
        )

        prompt = (
            "Given these database tables:\n"
            + "\n".join(f"  {name}" for name in table_names)
            + (f"\n\n{conversation}" if conversation else "")
            + f"\n\nQuestion: {question}\n\n"
            'Which tables are needed? Reply with JSON only: {"tables": ["name", ...]}. '
            "Include only names from the list above. Pick the smallest useful set."
        )

        try:
            completion = self.client.complete(prompt, model=self.config.light_model)
            trace.record(completion)
            payload = json_from_reply(completion.text)
            chosen = [
                name
                for name in payload.get("tables", [])
                if isinstance(name, str) and name in self.snapshot
            ]
            if chosen:
                return chosen
        except Exception as exc:  # noqa: BLE001 - fall back, never fail here
            logger.warning("seed table selection failed, falling back to matching: %s", exc)

        matched = self._match_tables_by_name(question, table_names)
        if matched:
            return matched

        # Nothing in the question named a table. On a follow-up that is the
        # normal case, not a failure — carry forward whatever the previous
        # turns queried.
        return mentioned_tables(list(history), set(table_names))

    @staticmethod
    def _match_tables_by_name(question: str, table_names: list[str]) -> list[str]:
        """Deterministic fallback: find table names mentioned in the question.

        Matches singular forms too, so "customer" finds the ``customers``
        table. Crude, but it only has to produce a starting point — the graph
        walk does the real work.
        """
        lowered = question.lower()
        return [
            name
            for name in table_names
            if name.lower() in lowered or name.lower().rstrip("s") in lowered
        ]

    def build_context(self, tables: list[str], trace: Trace) -> str:
        """Assemble the schema description for the prompt.

        Two modes, chosen by configuration: value profiling (what each column
        can contain) or plain row sampling (two whole rows). Profiling is the
        default because a filter needs vocabulary, not a specimen row.
        """
        if self.config.sample_rows <= 0:
            return render_schema(
                self.snapshot, self.graph, tables, dialect=self.dialect.sqlglot_name
            )

        if self.config.value_profiling:
            profiles = profile_tables(
                self.engine,
                tables,
                sample_rows=self.config.profile_rows,
                max_distinct=self.config.max_distinct_values,
                example_values=self.config.sample_rows,
                max_cell_chars=self.config.max_cell_chars,
                dialect=self.dialect,
            )
            trace.sampled = bool(profiles)
            return render_schema(
                self.snapshot,
                self.graph,
                tables,
                profiles=profiles,
                dialect=self.dialect.sqlglot_name,
            )

        samples = sample_tables(
            self.engine,
            tables,
            limit=self.config.sample_rows,
            max_cell_chars=self.config.max_cell_chars,
            dialect=self.dialect,
        )
        trace.sampled = bool(samples)
        return render_schema(
            self.snapshot,
            self.graph,
            tables,
            samples=samples,
            dialect=self.dialect.sqlglot_name,
        )

    def glossary_for(self, tables: list[str]) -> str:
        """The glossary text for a prompt about these tables.

        Narrowed to the tables in context: a fifty-column glossary on a
        two-table question is forty-eight lines competing with the schema for
        the model's attention.
        """
        return self.glossary.for_tables(set(tables)).render()

    def pick_model(self, tables: list[str]) -> str:
        """Choose a model tier for the question at hand.

        A single-table lookup does not need the larger model; a four-table join
        does. Table count is a crude proxy for difficulty, but it is free —
        deciding properly would itself cost a model call.
        """
        return self.config.light_model if len(tables) <= 2 else self.config.strong_model

    def write_answer(
        self,
        question: str,
        sql: str,
        result: QueryResult,
        trace: Trace,
        history: Sequence[Turn] = (),
        *,
        window: int | None = None,
        summary: str = "",
    ) -> str:
        """Turn rows into a sentence.

        Empty results are answered without a model call: there is nothing to
        summarise, and a model given no rows tends to invent an explanation.
        """
        if result.row_count == 0:
            return (
                "That query returned no rows. The data may not contain anything "
                "matching those filters."
            )

        preview = result.preview(limit=20, max_cell_chars=self.config.max_cell_chars)
        prompt = build_answer_prompt(
            question,
            sql,
            preview,
            # The answer needs the units as much as the query did: a correctly
            # converted figure can still be reported in the wrong currency.
            glossary=self.glossary.render(),
            conversation=render_conversation(
                list(history),
                window=self.config.conversation_window if window is None else window,
                summary=summary,
            ),
        )

        completion = self.client.complete(
            prompt, model=self.config.light_model, system=ANSWER_SYSTEM_PROMPT
        )
        trace.record(completion)
        answer = completion.text

        # The query ran correctly; that says nothing about whether the sentence
        # built from its rows is true. Observed on a real database: a one-row
        # result naming one item, described in the answer as a different item
        # entirely. See `sqlagent.guards.faithfulness`.
        if not self.config.check_answer_faithfulness:
            return answer

        correction = faithfulness.check(answer, result, question)
        if correction is None:
            return answer

        logger.warning("answer not supported by results, retrying: %s", correction)
        trace.answer_corrected = True

        retry = self.client.complete(
            f"{prompt}\n\nYour previous answer was:\n{answer}\n\n{correction}",
            model=self.config.light_model,
            system=ANSWER_SYSTEM_PROMPT,
        )
        trace.record(retry)

        # One retry, then accept what comes back. A second failure means the
        # check and the model disagree about what counts as supported, and
        # looping on that burns tokens without converging — the flag on the
        # trace is what makes the disagreement visible.
        if faithfulness.check(retry.text, result, question) is not None:
            logger.warning("answer still unsupported after one correction")
            trace.answer_unverified = True

        return retry.text


def _safe_hook(hook: ProgressHook | None) -> ProgressHook:
    """Wrap a progress hook so it can never break the request.

    A UI callback that raises — a closed websocket, a full queue — must not
    turn a successful answer into a failure.
    """
    if hook is None:
        return lambda stage, detail: None

    def safe(stage: str, detail: dict) -> None:
        try:
            hook(stage, detail)
        except Exception:  # noqa: BLE001 - progress reporting is best-effort
            logger.debug("progress hook failed for stage %s", stage, exc_info=True)

    return safe
