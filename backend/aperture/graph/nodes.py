"""Nodes of the analyst graph.

Live objects (engine, model, linker) live on the context rather than in state,
because state is serialised by the checkpointer on every step.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import time
from dataclasses import dataclass, field

from ..budget import LEDGER, BudgetExceeded
from ..cache import lookup as cache_lookup
from ..cache import remember as cache_remember
from ..charts import build_spec
from ..config import settings
from ..db import Database, SchemaBundle, load_schema
from ..db.connection import QueryFailed
from ..extract import NoSQLFound, extract_sql
from ..followups import suggest
from ..guards.cost import estimate_cost
from ..guards.validator import validate_sql
from ..insights import find_insights
from ..llm import build_llm, invoke_metered, stop_reason, usage_of
from ..schema import SchemaLinker
from ..semantic import SemanticLayer
from ..sqlfix import repair_identifiers
from ..telemetry import record_run
from ..verify import verify as verify_query
from ..vote import gather
from .empty import diagnose_empty
from .prompts import generate_prompt, narrate_prompt, repair_prompt
from .state import AnalystState

log = logging.getLogger(__name__)

GREETINGS = {"hi", "hello", "hey", "yo", "thanks", "thank you", "bye"}
META_MARKERS = ("what can you do", "who are you", "help", "what is this", "capabilities")


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


# Guidance specific to a failure class. Postgres names the problem precisely in
# its sqlstate; repeating the raw message alone leaves the model to rediscover
# the rule it broke.
CORRECTIVES = {
    # undefined table / missing FROM-clause entry
    "42P01": (
        "Reference only tables and CTEs that appear in your own FROM or JOIN clauses. "
        "Every alias you use on the left of a dot must be defined there."
    ),
    # undefined column
    "42703": (
        "Use only the column names shown in the schema above, spelled exactly, and "
        "double-quote any camelCase identifier."
    ),
    # grouping error
    "42803": (
        "Every selected expression must appear in GROUP BY or be inside an aggregate "
        "function."
    ),
    # invalid literal for the column's type, typically an enum
    "22P02": (
        "Use only the literal values listed under VALUE HINTS; the value you used is not "
        "valid for that column's type."
    ),
    "42702": "Qualify every ambiguous column with its table alias.",
    "57014": "The query timed out. Narrow it: filter harder or aggregate earlier.",
    "too_expensive": "Add a filter or aggregate so the query scans less data.",
}

SIMPLIFY = (
    "Prefer the simplest formulation that answers the question: a single SELECT with "
    "plain aggregates, avoiding CTEs and cross joins."
)


def corrective_for(
    error_kind: str, *, attempts: int, max_attempts: int, tables: list[str] | None = None
) -> str:
    """Build the guidance shown alongside a failed query."""
    note = CORRECTIVES.get(error_kind, "")
    if error_kind == "42P01" and tables:
        note += " Available tables: " + ", ".join(tables[:12]) + "."
    # Complexity is its own failure mode: on the final attempt, ask for the
    # simplest thing that could answer the question.
    if attempts >= max_attempts - 1:
        note = f"{note} {SIMPLIFY}".strip()
    return note.strip()


@dataclass
class AnalystContext:
    """Everything the nodes need that must not be serialised into state."""

    db: Database
    bundle: SchemaBundle
    linker: SchemaLinker
    semantic: SemanticLayer = field(default_factory=SemanticLayer.default)
    trace_sink: list = field(default_factory=list)

    @classmethod
    def create(cls, db: Database | None = None, *, refresh: bool = False) -> AnalystContext:
        db = db or Database.active()
        bundle = load_schema(db, refresh=refresh)
        linker = SchemaLinker(bundle.snapshot, bundle.profile)
        columns_by_table = {
            name: {column.name for column in table.columns}
            for name, table in bundle.snapshot.tables.items()
        }
        semantic = SemanticLayer.default().for_schema(columns_by_table)
        return cls(db=db, bundle=bundle, linker=linker, semantic=semantic)

    @property
    def dialect(self) -> str:
        return self.db.sqlglot_dialect

    @property
    def schema_version(self) -> str:
        """Changes when the schema does, so cached SQL is invalidated."""
        shape = ";".join(
            f"{name}:{len(table.columns)}"
            for name, table in sorted(self.bundle.snapshot.tables.items())
        )
        import hashlib as _hashlib

        return _hashlib.sha256(shape.encode()).hexdigest()[:12]


def _over_deadline(state: AnalystState) -> bool:
    started = state.get("started_at") or time.time()
    return (time.time() - started) > settings().run_deadline_seconds


def _note(state: AnalystState, node: str, **fields) -> list[dict]:
    trace = list(state.get("trace", []))
    trace.append({"node": node, "at": round(time.time(), 3), **fields})
    return trace


def make_nodes(ctx: AnalystContext) -> dict:
    cfg = settings()

    def _persist(state: AnalystState) -> None:
        record_run(
            dict(state),
            dataset=ctx.db.fingerprint,
            dialect=ctx.db.dialect,
            cost_usd=LEDGER.spent_usd(),
        )

    def route(state: AnalystState) -> AnalystState:
        question = (state.get("question") or "").strip()
        lowered = question.lower().rstrip("?!. ")
        if not question or lowered in GREETINGS or len(lowered) < 3:
            intent = "chitchat"
        elif any(marker in lowered for marker in META_MARKERS):
            intent = "schema_question"
        else:
            intent = "query"
        # Every field the previous turn wrote must be cleared here. The
        # checkpointer restores the whole thread state, so a leftover terminal
        # status from the last question ends this one the moment it starts --
        # the query is generated, then skipped straight to the end unexecuted.
        return {
            "intent": intent,
            "started_at": time.time(),
            "attempts": 0,
            "empty_retries": 0,
            "seen_sql_hashes": [],
            "seen_error_keys": [],
            "status": "pending",
            "sql": "",
            "raw_response": "",
            "answer": "",
            "assumptions": "",
            "diagnosis": "",
            "last_error": "",
            "last_error_kind": "",
            "repair_note": "",
            "identifier_fixes": [],
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0.0,
            "estimated_cost": None,
            "chart_spec": None,
            "tokens_used": 0,
            "trace": [{"node": "route", "at": round(time.time(), 3), "intent": intent}],
        }

    def small_talk(state: AnalystState) -> AnalystState:
        if state.get("intent") == "schema_question":
            tables = ctx.bundle.snapshot.table_names
            rows = sum(t.exact_rows for t in ctx.bundle.profile.tables.values())
            answer = (
                f"I answer questions about this database in plain English. It has "
                f"{len(tables)} tables and about {rows:,} rows in total; the busiest are "
                + ", ".join(
                    name
                    for name, _ in sorted(
                        ((n, p.exact_rows) for n, p in ctx.bundle.profile.tables.items()),
                        key=lambda pair: -pair[1],
                    )[:5]
                )
                + ". Ask something like: how many orders were delivered last month?"
            )
        else:
            answer = "Ask me a question about the data and I'll write the SQL for it."
        update = {"answer": answer, "status": "answered", "trace": _note(state, "small_talk")}
        _persist({**state, **update})
        return update

    def link_schema(state: AnalystState) -> AnalystState:
        matched = ctx.semantic.match(state["question"])
        metric_tables = [table for metric in matched for table in metric.tables]
        linked = ctx.linker.link(state["question"], extra_seeds=metric_tables)
        section = linked.as_prompt_section()
        # Business definitions go last so they are the final word before the
        # question, and only the ones this question touches are included.
        metrics = ctx.semantic.prompt_section(state["question"])
        if metrics:
            section = f"{section}\n\n{metrics}"
        return {
            "linked_tables": linked.tables,
            "schema_section": section,
            "value_hints": linked.value_hints,
            "empty_tables": linked.empty_tables,
            "trace": _note(
                state,
                "link_schema",
                tables=len(linked.tables),
                seeds=linked.seeds[:5],
                metrics=[m.name for m in ctx.semantic.match(state["question"])],
            ),
        }

    def generate_sql(state: AnalystState) -> AnalystState:
        if _over_deadline(state):
            update: AnalystState = {
                "status": "timed_out",
                "answer": "Timed out before producing an answer.",
                "trace": _note(state, "generate_sql", skipped="deadline"),
            }
            _persist({**state, **update})
            return update

        attempts = state.get("attempts", 0)
        repeated = state.get("repair_note", "").startswith("IDENTICAL")
        # A repeat at temperature 0 will reproduce itself exactly; nudge it.
        temperature = cfg.repair_temperature if repeated else None
        max_tokens = cfg.max_tokens * 2 if state.get("last_error_kind") == "truncated" else None

        if state.get("last_error"):
            messages = repair_prompt(
                state["question"],
                state["schema_section"],
                state.get("sql", ""),
                state["last_error"],
                ctx.dialect,
                note=state.get("repair_note", ""),
            )
            label = f"repair-{attempts}"
        else:
            messages = generate_prompt(
                state["question"],
                state["schema_section"],
                ctx.dialect,
                history=state.get("history"),
            )
            label = "generate"

        # First attempt with voting enabled: sample several candidates, run
        # them, and let agreement between independently written queries decide.
        # Repairs stay single-shot -- there the database has already said what
        # is wrong, so sampling adds cost without adding information.
        first_attempt = not state.get("last_error")
        if first_attempt:
            cached = cache_lookup(
                state["question"], ctx.db.fingerprint, ctx.schema_version
            )
            if cached:
                # The query is reused; the data is always read fresh.
                return {
                    "sql": cached.sql,
                    "assumptions": cached.assumptions,
                    "cache_hit": True,
                    "last_error": "",
                    "last_error_kind": "",
                    "trace": _note(state, "generate_sql", cached=True, hits=cached.hits),
                }
        if first_attempt and cfg.candidates > 1:
            voted = _vote_on_candidates(state, messages)
            if voted is not None:
                return voted

        llm = build_llm(temperature=temperature, max_tokens=max_tokens)
        try:
            response = invoke_metered(llm, messages, label=label)
        except BudgetExceeded as err:
            update = {
                "status": "over_budget",
                "answer": f"Stopped: {err}",
                "trace": _note(state, "generate_sql", error="budget"),
            }
            _persist({**state, **update})
            return update

        text = response.content if isinstance(response.content, str) else str(response.content)
        tokens_in, tokens_out = usage_of(response)
        update: AnalystState = {
            "raw_response": text,
            "tokens_used": state.get("tokens_used", 0) + tokens_in + tokens_out,
            "trace": _note(state, "generate_sql", label=label, tokens=tokens_in + tokens_out),
        }

        if stop_reason(response) == "max_tokens":
            # Truncated SQL is not a repairable error; it is a budget problem.
            update.update(
                last_error="The response was cut off before the query was complete.",
                last_error_kind="truncated",
                repair_note="",
            )
            return update

        assumptions = ""
        for line in text.splitlines():
            if line.strip().upper().startswith("ASSUMPTIONS:"):
                assumptions = line.split(":", 1)[1].strip()
                break

        try:
            sql = extract_sql(text, dialect=ctx.dialect)
        except NoSQLFound:
            update.update(
                sql="",
                last_error="Your reply contained no SQL statement.",
                last_error_kind="no_sql",
                repair_note="Respond with only a SQL statement inside a ```sql block.",
            )
            return update

        update.update(sql=sql, assumptions=assumptions or state.get("assumptions", ""))
        return update

    def _draft(messages: list, temperature: float) -> str | None:
        """One candidate query, or None if the model did not return SQL."""
        try:
            response = invoke_metered(
                build_llm(temperature=temperature), messages, label="candidate"
            )
        except BudgetExceeded:
            raise
        except Exception as err:
            log.debug("candidate generation failed: %s", err)
            return None
        text = response.content if isinstance(response.content, str) else str(response.content)
        if stop_reason(response) == "max_tokens":
            return None
        try:
            return extract_sql(text, dialect=ctx.dialect)
        except NoSQLFound:
            return None

    def _vote_on_candidates(state: AnalystState, messages: list) -> AnalystState | None:
        """Sample candidates and pick the result most of them agree on.

        Returns None when voting produced nothing runnable, so the caller falls
        back to a single generation and the ordinary repair loop.
        """
        temperatures = [0.0] + [cfg.candidate_temperature] * (cfg.candidates - 1)
        try:
            vote = gather(
                lambda temperature: _draft(messages, temperature),
                temperatures,
                db=ctx.db,
                snapshot=ctx.bundle.snapshot,
                dialect=ctx.dialect,
                row_limit=cfg.row_limit,
            )
        except BudgetExceeded as err:
            return {
                "status": "over_budget",
                "answer": f"Stopped: {err}",
                "trace": _note(state, "generate_sql", error="budget"),
            }

        if not vote.winner:
            failed = next((c for c in vote.candidates if c.error), None)
            if not failed:
                return None
            # Every candidate failed the same way; hand the error to the loop.
            return {
                "sql": failed.sql,
                "last_error": failed.error,
                "last_error_kind": failed.error_kind,
                "trace": _note(state, "generate_sql", candidates=vote.considered, runnable=0),
            }

        winner = vote.winner
        return {
            "sql": winner.sql,
            "columns": winner.columns,
            "rows": winner.rows,
            "row_count": winner.row_count,
            "vote_agreement": vote.agreement,
            "vote_considered": vote.considered,
            "last_error": "",
            "last_error_kind": "",
            "trace": _note(
                state,
                "generate_sql",
                candidates=vote.considered,
                runnable=vote.executable,
                agreement=vote.agreement,
            ),
        }

    def validate(state: AnalystState) -> AnalystState:
        sql = state.get("sql", "")
        if not sql:
            return {"trace": _note(state, "validate", skipped="no sql")}

        result = validate_sql(sql, dialect=ctx.dialect, row_limit=cfg.row_limit)
        trace = _note(state, "validate", ok=result.ok, kind=result.kind)

        if result.ok:
            # A table the schema does not contain cannot be fixed by executing
            # it and reading the error; check membership here and name the real
            # tables, which is what the model needed in the first place.
            known = set(ctx.bundle.snapshot.tables)
            unknown = [t for t in result.tables if t not in known]
            if unknown:
                suggestions = []
                for name in unknown:
                    close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
                    if close:
                        suggestions.append(f"{name} (did you mean: {', '.join(close)}?)")
                    else:
                        suggestions.append(name)
                return {
                    "last_error": (
                        "These tables do not exist in this database: "
                        + "; ".join(suggestions)
                    ),
                    "last_error_kind": "42P01",
                    "repair_note": "",
                    "trace": _note(state, "validate", ok=False, kind="unknown_table"),
                }

            # Correct case-folded identifiers before the database rejects them.
            # The schema already knows the real spelling, so spending a repair
            # attempt to rediscover it is waste.
            fix = repair_identifiers(result.sql, ctx.bundle.snapshot, dialect=ctx.dialect)
            if fix.applied:
                trace = _note(state, "validate", ok=True, kind="ok", fixed=fix.changed)
            return {
                "sql": fix.sql,
                "identifier_fixes": state.get("identifier_fixes", []) + fix.changed,
                "last_error": "",
                "last_error_kind": "",
                "trace": trace,
            }

        if result.kind in {"write", "banned_function", "locking"}:
            update: AnalystState = {
                "status": "refused",
                "answer": (
                    f"Refused: {result.reason}. Aperture connects with a read-only "
                    "role, so this cannot be executed."
                ),
                "trace": trace,
            }
            _persist({**state, **update})
            return update

        return {
            "last_error": result.reason,
            "last_error_kind": result.kind,
            "repair_note": "Return a single read-only SELECT statement.",
            "trace": trace,
        }

    def cost_guard(state: AnalystState) -> AnalystState:
        estimate = estimate_cost(ctx.db, state["sql"])
        trace = _note(state, "cost_guard", kind=estimate.kind, cost=estimate.total_cost)

        if estimate.kind == "ok":
            return {"estimated_cost": estimate.total_cost, "last_error": "", "trace": trace}

        if estimate.kind == "unsupported":
            return {"trace": trace}

        if estimate.kind == "too_expensive":
            return {
                "last_error": estimate.reason,
                "last_error_kind": "too_expensive",
                "repair_note": "Add a filter or aggregate so the query scans less data.",
                "trace": trace,
            }

        error = estimate.error
        if error and error.is_infrastructure:
            return _unavailable(state, error, "cost_guard")
        return {
            "last_error": error.for_prompt() if error else estimate.reason,
            # Keep the sqlstate: "invalid" says the SQL is wrong, the code says how.
            "last_error_kind": (error.sqlstate if error and error.sqlstate else "invalid"),
            "repair_note": "",
            "trace": trace,
        }

    def _unavailable(state: AnalystState, error, where: str) -> AnalystState:
        """Terminate cleanly: the database is unreachable, the query is fine."""
        update: AnalystState = {
            "status": "unavailable",
            "answer": (
                f"The database could not be reached, so the question was not run.\n\n"
                f"{error.primary or error.raw}\n\n{error.advice}"
            ),
            "last_error": error.for_prompt(),
            "last_error_kind": error.sqlstate or "unavailable",
            "trace": _note(state, where, unavailable=True),
        }
        _persist({**state, **update})
        return update

    def execute(state: AnalystState) -> AnalystState:
        if _over_deadline(state):
            update: AnalystState = {
                "status": "timed_out",
                "answer": "Timed out before the query finished.",
                "trace": _note(state, "execute", skipped="deadline"),
            }
            _persist({**state, **update})
            return update
        try:
            result = ctx.db.run(state["sql"])
        except QueryFailed as err:
            if err.error.is_infrastructure:
                return _unavailable(state, err.error, "execute")
            return {
                "last_error": err.error.for_prompt(),
                "last_error_kind": err.error.sqlstate or "db_error",
                "repair_note": "",
                "trace": _note(state, "execute", error=err.error.sqlstate),
            }

        return {
            "columns": result.columns,
            "rows": [list(r) for r in result.rows],
            "row_count": result.row_count,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
            "last_error": "",
            "last_error_kind": "",
            "trace": _note(state, "execute", rows=result.row_count, ms=round(result.elapsed_ms)),
        }

    def diagnose(state: AnalystState) -> AnalystState:
        """Decide whether, and how, to try again."""
        attempts = state.get("attempts", 0)
        sql_hash = _hash(state.get("sql", ""))
        seen_sql = list(state.get("seen_sql_hashes", []))
        seen_errors = list(state.get("seen_error_keys", []))
        error_key = f"{state.get('last_error_kind')}|{state.get('last_error', '')[:120]}"

        note = corrective_for(
            state.get("last_error_kind", ""),
            attempts=attempts,
            max_attempts=cfg.max_repair_attempts,
            tables=state.get("linked_tables", []),
        )
        # An identical query means the prompt failed to change anything, so the
        # attempt is not charged -- but something must change before retrying.
        identical = sql_hash in seen_sql
        if identical:
            note = (
                "IDENTICAL: you returned the same query that just failed. Change it. "
                + note
            )
        else:
            seen_sql.append(sql_hash)
            attempts += 1

        if error_key in seen_errors:
            # Repeating the same error means editing the SQL is the wrong move;
            # widen the schema instead of arguing with the model.
            linked = ctx.linker.link(
                state["question"] + " " + " ".join(state.get("linked_tables", [])[:3])
            )
            note = (
                "The same error occurred twice. Here is a wider view of the schema; "
                "re-read the exact column spellings."
            )
            return {
                "attempts": attempts,
                "seen_sql_hashes": seen_sql,
                "seen_error_keys": seen_errors,
                "schema_section": linked.as_prompt_section(),
                "linked_tables": linked.tables,
                "repair_note": note,
                "trace": _note(state, "diagnose", action="widen_schema", attempts=attempts),
            }

        seen_errors.append(error_key)
        return {
            "attempts": attempts,
            "seen_sql_hashes": seen_sql,
            "seen_error_keys": seen_errors,
            "repair_note": note,
            "trace": _note(state, "diagnose", attempts=attempts, identical=identical),
        }

    def diagnose_empty_node(state: AnalystState) -> AnalystState:
        finding = diagnose_empty(
            state.get("sql", ""),
            state.get("linked_tables", []),
            ctx.bundle.snapshot,
            ctx.bundle.profile,
            dialect=ctx.dialect,
        )
        trace = _note(state, "diagnose_empty", found=bool(finding), retryable=finding.retryable)

        if not finding:
            return {"diagnosis": "", "trace": trace}

        retries = state.get("empty_retries", 0)
        # A scalar zero is already an answer; only a genuinely empty result set
        # is worth rewriting for.
        rewritable = state.get("row_count", 0) == 0
        if finding.retryable and rewritable and retries < cfg.max_empty_retries:
            return {
                "diagnosis": finding.explanation,
                "empty_retries": retries + 1,
                "last_error": finding.explanation,
                "last_error_kind": "empty_result",
                "repair_note": finding.suggestion,
                "trace": trace,
            }

        return {"diagnosis": finding.explanation, "trace": trace}

    def narrate(state: AnalystState) -> AnalystState:
        rows = state.get("rows", [])
        columns = state.get("columns", [])
        row_count = state.get("row_count", 0)

        if row_count == 0:
            diagnosis = state.get("diagnosis")
            answer = diagnosis or "The query ran successfully and matched no rows."
            return {"answer": answer, "status": "empty", "trace": _note(state, "narrate", empty=True)}

        try:
            response = invoke_metered(
                build_llm(max_tokens=400),
                narrate_prompt(
                    state["question"],
                    state.get("sql", ""),
                    columns,
                    rows,
                    row_count,
                    conventions=ctx.semantic.conventions,
                ),
                label="narrate",
            )
        except BudgetExceeded as err:
            return {"answer": f"Query succeeded; narration stopped: {err}", "status": "answered",
                    "trace": _note(state, "narrate", error="budget")}

        text = response.content if isinstance(response.content, str) else str(response.content)
        answer = text.strip()
        # A count of zero is a correct answer and a confusing one; say why.
        if state.get("diagnosis"):
            answer = f"{answer}\n\n{state['diagnosis']}"
        # Caveats are appended rather than used to rewrite the query: the SQL is
        # on screen, and a warning the user can weigh beats a silent correction.
        for finding in state.get("verification", []):
            answer = f"{answer}\n\nCaveat: {finding['message']}"
        return {
            "answer": answer,
            "status": "answered",
            "tokens_used": state.get("tokens_used", 0) + sum(usage_of(response)),
            "trace": _note(state, "narrate", rows=row_count),
        }

    def _remember_query(state: AnalystState) -> None:
        if state.get("cache_hit") or not state.get("sql"):
            return
        cache_remember(
            state.get("question", ""),
            ctx.db.fingerprint,
            ctx.schema_version,
            state["sql"],
            state.get("assumptions", ""),
        )

    def verify_result(state: AnalystState) -> AnalystState:
        _remember_query(state)
        findings = verify_query(
            ctx.db, state.get("sql", ""), ctx.bundle.snapshot, dialect=ctx.dialect
        )
        patterns = find_insights(state.get("columns") or [], state.get("rows") or [])
        return {
            "verification": [{"kind": f.kind, "message": f.message} for f in findings],
            "insights": [{"kind": i.kind, "message": i.message} for i in patterns],
            "trace": _note(
                state,
                "verify",
                findings=[f.kind for f in findings],
                insights=[i.kind for i in patterns],
            ),
        }

    def chart(state: AnalystState) -> AnalystState:
        rows = state.get("rows") or []
        columns = state.get("columns") or []
        spec = (
            build_spec(columns, rows, title=state.get("question", "")[:80])
            if rows and columns
            else None
        )
        suggestions = suggest(
            question=state.get("question", ""),
            columns=columns,
            rows=rows,
            linked_tables=state.get("linked_tables", []),
            snapshot=ctx.bundle.snapshot,
            profile=ctx.bundle.profile,
            status=state.get("status", "answered"),
            has_caveat=bool(state.get("verification")),
        )
        update = {
            "chart_spec": spec,
            "suggestions": [{"text": s.text, "reason": s.reason} for s in suggestions],
            "trace": _note(state, "chart", spec=bool(spec)),
        }
        _persist({**state, **update})
        return update

    def exhausted(state: AnalystState) -> AnalystState:
        update: AnalystState = {
            "status": "exhausted",
            "answer": (
                "I could not produce a working query after "
                f"{state.get('attempts', 0)} attempts. Last error:\n{state.get('last_error', '')}"
            ),
            "trace": _note(state, "exhausted"),
        }
        _persist({**state, **update})
        return update

    return {
        "route": route,
        "small_talk": small_talk,
        "link_schema": link_schema,
        "generate_sql": generate_sql,
        "validate": validate,
        "cost_guard": cost_guard,
        "execute": execute,
        "diagnose": diagnose,
        "diagnose_empty": diagnose_empty_node,
        "verify": verify_result,
        "narrate": narrate,
        "chart": chart,
        "exhausted": exhausted,
    }
