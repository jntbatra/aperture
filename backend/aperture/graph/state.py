"""State carried through the analyst graph.

Everything here is a plain value: the checkpointer serialises state on every
step, so live objects (engines, models, linkers) live in the context object
instead, not in state.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Status = Literal[
    "pending",
    "answered",
    "refused",
    "empty",
    "exhausted",
    "over_budget",
    "timed_out",
]


class AnalystState(TypedDict, total=False):
    # input
    question: str
    thread_id: str
    # earlier turns in this conversation: [{question, sql}]
    history: list[dict]

    # routing
    intent: Literal["query", "chitchat", "schema_question"]

    # linked schema, flattened for serialisation
    linked_tables: list[str]
    schema_section: str
    value_hints: list[str]
    empty_tables: list[str]

    # generation and repair
    raw_response: str
    sql: str
    attempts: int
    empty_retries: int
    # Hashes of SQL already tried. A repeat means the prompt failed to change
    # anything, which is a prompt problem, not a model problem.
    seen_sql_hashes: list[str]
    # (sqlstate, message) pairs already seen. A repeat means repairing the SQL
    # is the wrong move and the strategy has to change.
    seen_error_keys: list[str]
    last_error: str
    last_error_kind: str
    repair_note: str
    # Identifiers rewritten deterministically from the schema, e.g. createdat -> "createdAt"
    identifier_fixes: list[str]
    # Self-consistency: how many candidates agreed with the chosen result
    vote_agreement: int
    vote_considered: int
    cache_hit: bool

    # execution
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float
    estimated_cost: float | None

    # output
    diagnosis: str
    assumptions: str
    # Deterministic post-execution checks: fan-out, dropped groups
    verification: list[dict]
    insights: list[dict]
    clarifying_question: str
    clarify_options: list[str]
    suggestions: list[dict]
    answer: str
    chart_spec: dict | None
    status: Status

    # accounting
    started_at: float
    tokens_used: int
    trace: list[dict]
