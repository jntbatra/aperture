"""Nodes of the analyst graph.

Live objects (engine, model, linker) live on the context rather than in state,
because state is serialised by the checkpointer on every step.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

from ..budget import LEDGER, BudgetExceeded
from ..charts import build_spec
from ..config import settings
from ..db import Database, SchemaBundle, load_schema
from ..db.connection import QueryFailed
from ..extract import NoSQLFound, extract_sql
from ..guards.cost import estimate_cost
from ..guards.validator import validate_sql
from ..llm import build_llm, invoke_metered, stop_reason, usage_of
from ..schema import SchemaLinker
from ..sqlfix import repair_identifiers
from .empty import diagnose_empty
from .prompts import generate_prompt, narrate_prompt, repair_prompt
from .state import AnalystState

log = logging.getLogger(__name__)

GREETINGS = {"hi", "hello", "hey", "yo", "thanks", "thank you", "bye"}
META_MARKERS = ("what can you do", "who are you", "help", "what is this", "capabilities")


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


@dataclass
class AnalystContext:
    """Everything the nodes need that must not be serialised into state."""

    db: Database
    bundle: SchemaBundle
    linker: SchemaLinker
    trace_sink: list = field(default_factory=list)

    @classmethod
    def create(cls, db: Database | None = None, *, refresh: bool = False) -> "AnalystContext":
        db = db or Database.from_settings()
        bundle = load_schema(db, refresh=refresh)
        linker = SchemaLinker(bundle.snapshot, bundle.profile)
        return cls(db=db, bundle=bundle, linker=linker)

    @property
    def dialect(self) -> str:
        return self.db.sqlglot_dialect


def _over_deadline(state: AnalystState) -> bool:
    started = state.get("started_at") or time.time()
    return (time.time() - started) > settings().run_deadline_seconds


def _note(state: AnalystState, node: str, **fields) -> list[dict]:
    trace = list(state.get("trace", []))
    trace.append({"node": node, "at": round(time.time(), 3), **fields})
    return trace


def make_nodes(ctx: AnalystContext) -> dict:
    cfg = settings()

    def route(state: AnalystState) -> AnalystState:
        question = (state.get("question") or "").strip()
        lowered = question.lower().rstrip("?!. ")
        if not question:
            intent = "chitchat"
        elif lowered in GREETINGS or len(lowered) < 3:
            intent = "chitchat"
        elif any(marker in lowered for marker in META_MARKERS):
            intent = "schema_question"
        else:
            intent = "query"
        return {
            "intent": intent,
            "started_at": state.get("started_at") or time.time(),
            "attempts": 0,
            "empty_retries": 0,
            "seen_sql_hashes": [],
            "seen_error_keys": [],
            "trace": _note(state, "route", intent=intent),
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
        return {"answer": answer, "status": "answered", "trace": _note(state, "small_talk")}

    def link_schema(state: AnalystState) -> AnalystState:
        linked = ctx.linker.link(state["question"])
        return {
            "linked_tables": linked.tables,
            "schema_section": linked.as_prompt_section(),
            "value_hints": linked.value_hints,
            "empty_tables": linked.empty_tables,
            "trace": _note(state, "link_schema", tables=len(linked.tables), seeds=linked.seeds[:5]),
        }

    def generate_sql(state: AnalystState) -> AnalystState:
        if _over_deadline(state):
            return {"status": "timed_out", "answer": "Timed out before producing an answer.",
                    "trace": _note(state, "generate_sql", skipped="deadline")}

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
            messages = generate_prompt(state["question"], state["schema_section"], ctx.dialect)
            label = "generate"

        llm = build_llm(temperature=temperature, max_tokens=max_tokens)
        try:
            response = invoke_metered(llm, messages, label=label)
        except BudgetExceeded as err:
            return {"status": "over_budget", "answer": f"Stopped: {err}",
                    "trace": _note(state, "generate_sql", error="budget")}

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

    def validate(state: AnalystState) -> AnalystState:
        sql = state.get("sql", "")
        if not sql:
            return {"trace": _note(state, "validate", skipped="no sql")}

        result = validate_sql(sql, dialect=ctx.dialect, row_limit=cfg.row_limit)
        trace = _note(state, "validate", ok=result.ok, kind=result.kind)

        if result.ok:
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
            return {
                "status": "refused",
                "answer": (
                    f"Refused: {result.reason}. Aperture connects with a read-only "
                    "role, so this cannot be executed."
                ),
                "trace": trace,
            }

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
        return {
            "last_error": error.for_prompt() if error else estimate.reason,
            "last_error_kind": "invalid",
            "repair_note": "",
            "trace": trace,
        }

    def execute(state: AnalystState) -> AnalystState:
        if _over_deadline(state):
            return {"status": "timed_out", "answer": "Timed out before the query finished.",
                    "trace": _note(state, "execute", skipped="deadline")}
        try:
            result = ctx.db.run(state["sql"])
        except QueryFailed as err:
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

        note = ""
        # An identical query means the prompt failed to change anything, so the
        # attempt is not charged -- but something must change before retrying.
        identical = sql_hash in seen_sql
        if identical:
            note = "IDENTICAL: you returned the same query that just failed. Change it."
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
                narrate_prompt(state["question"], state.get("sql", ""), columns, rows, row_count),
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
        return {
            "answer": answer,
            "status": "answered",
            "tokens_used": state.get("tokens_used", 0) + sum(usage_of(response)),
            "trace": _note(state, "narrate", rows=row_count),
        }

    def chart(state: AnalystState) -> AnalystState:
        rows = state.get("rows") or []
        columns = state.get("columns") or []
        if not rows or not columns:
            return {"chart_spec": None, "trace": _note(state, "chart", spec=False)}
        spec = build_spec(columns, rows, title=state.get("question", "")[:80])
        return {
            "chart_spec": spec,
            "trace": _note(state, "chart", spec=bool(spec)),
        }

    def exhausted(state: AnalystState) -> AnalystState:
        return {
            "status": "exhausted",
            "answer": (
                "I could not produce a working query after "
                f"{state.get('attempts', 0)} attempts. Last error:\n{state.get('last_error', '')}"
            ),
            "trace": _note(state, "exhausted"),
        }

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
        "narrate": narrate,
        "chart": chart,
        "exhausted": exhausted,
    }
