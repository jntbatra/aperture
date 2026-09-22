"""HTTP API.

Reading this if you know Express
--------------------------------
FastAPI plays the role Express does, with three differences worth knowing up
front.

**Routes.** In Express you write ``app.post('/api/ask', handler)``. Here you
decorate a function:

    @app.post("/api/ask")
    def ask(request: AskRequest) -> AskResponse: ...

**Bodies are validated for you.** Express hands you ``req.body`` as whatever
JSON arrived, and checking it is your problem. FastAPI reads the type
annotation — ``request: AskRequest`` — parses the body into that model, and
returns a 422 with a precise error if it does not fit. The handler only ever
runs with valid input. It is roughly ``express.json()`` plus zod, built in.

**Responses are objects, not ``res.send``.** You return a value; FastAPI
serialises it. There is no ``res`` to forget to call, so a handler cannot hang
by falling off the end.

**Dependencies replace middleware** for per-request setup. ``Depends(get_agent)``
means "call ``get_agent`` first, pass me the result". Comparable to attaching
something to ``req`` in Express middleware, but explicit at the point of use
and typed.

Two ways to ask a question
--------------------------
``POST /api/ask`` answers in one JSON response. Simple, and right for scripts.

``GET /api/ask/stream`` returns Server-Sent Events, so the browser can show
progress — "working out which tables are involved", "running the query" — while
the eight-odd seconds of model calls happen. SSE is a plain HTTP response that
stays open and emits ``data: ...`` lines; unlike a websocket it needs no
special server support and reconnects on its own.

Why the agent runs in a thread
------------------------------
The pipeline is ordinary blocking code — database calls, HTTP calls to Bedrock.
Calling it directly inside an ``async def`` would block the event loop and
freeze every other request. ``run_in_threadpool`` hands it to a worker thread
and awaits the result, which keeps the server responsive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from starlette.concurrency import run_in_threadpool

from sqlagent.api.auth_routes import (
    PrincipalDep,
    enforce_and_clamp,
    record_question,
)
from sqlagent.api.auth_routes import router as auth_router
from sqlagent.clarify import CLARIFICATION_ERROR
from sqlagent.config import OVERRIDABLE, Settings, settings
from sqlagent.conversation import Turn, carryable_result
from sqlagent.ingest import IngestError, ingest_file
from sqlagent.pipeline import AgentResult, SqlAgent
from sqlagent.schema.graph import EDGE_FOREIGN_KEY
from sqlagent.store import Store

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Request and response shapes
# --------------------------------------------------------------------------


class AskOptions(BaseModel):
    """Per-question overrides for the quality toggles.

    Only these. The database, the models, the row cap and the timeouts stay
    server-side — a client that could set its own row limit would not be
    configuring a feature, it would be removing a control. The allow-list is
    enforced again in ``config.apply_overrides``, so this model being wrong
    cannot widen it.

    Every field defaults to None, meaning "use the server's setting". That
    distinction matters: ``False`` means "turn this off for this question", and
    a model that could not express "unset" would force every client to send a
    full configuration on every request.
    """

    initial_hops: int | None = Field(default=None, ge=0, le=3)
    """How far to walk the foreign-key graph when choosing which tables to show."""

    max_hops: int | None = Field(default=None, ge=0, le=4)
    quality_tier: Literal["fast", "medium", "thorough"] | None = None
    use_critic: bool | None = None
    vote_samples: int | None = Field(default=None, ge=1, le=7)
    prescreen_input: bool | None = None
    ambiguity_handling: Literal["best_effort", "ask_human"] | None = None
    decompose_questions: bool | None = None
    cache_sql: bool | None = None

    summarise_conversation: bool | None = None
    conversation_window: int | None = Field(default=None, ge=0, le=20)
    max_clarifying_questions: int | None = Field(default=None, ge=1, le=10)
    """Ceiling on how many things the agent asks at once. At least 1: a
    clarification with nothing in it stops the user and tells them nothing."""
    """How many turns the model sees in full. Capped at 20 here as well as in
    the settings: an unbounded window is a prompt that grows until the schema is
    squeezed out, and that is not something a client should be able to do."""

    def overrides(self) -> dict:
        """Only the fields actually set."""
        return self.model_dump(exclude_none=True)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    dataset_id: str | None = None
    """Ask against an uploaded dataset instead of the configured database."""

    options: AskOptions | None = None
    """Quality toggles for this question only."""

    conversation_id: str | None = None
    """Continue an existing thread, so the question may refer back to it.

    ``None`` starts a new one. The id comes back in the response, and the client
    sends it with the next question — which is what turns a sequence of
    unrelated queries into a conversation where "and for April?" is answerable.
    """


class AskResponse(BaseModel):
    """What the client gets back.

    Includes the SQL deliberately. Showing the query that produced an answer is
    the difference between a tool an analyst can trust and one they cannot
    check — and it costs nothing to expose, since the query already ran.
    """

    question: str
    answer: str
    conversation_id: str | None = None
    """The thread this turn belongs to. Echoed back so the client can send it
    with the next question without having to create the thread itself."""

    sql: str | None
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    ok: bool
    error: str | None
    # Diagnostics, useful in the UI and essential in evaluation.
    repairs: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float
    tables_considered: list[str]

    clarification_asks: list[dict] = []
    """Set when the agent asked instead of answering: one entry per ambiguity,
    each with its own options, so the client can offer them as buttons rather
    than a text box. A user shown "(a / b)" and given somewhere to type answers
    "Yes" — observed."""

    warnings: list[str] = []
    """Things that ran successfully but may still be wrong.

    A query can execute perfectly and return a number that is not the number
    anyone wanted — an aggregate multiplied by a join, an answer the results do
    not support. Those checks already existed and already fired; they wrote to
    the server log, which nobody reads, and the client was told nothing.

    A guard whose finding never reaches the person acting on the figure is not a
    guard. This is the field that carries it.
    """


class TableInfo(BaseModel):
    name: str
    columns: list[str]
    primary_key: list[str]
    references: list[str]


class SchemaResponse(BaseModel):
    version: str
    table_count: int
    tables: list[TableInfo]


class GraphNode(BaseModel):
    id: str
    label: str
    columns: int
    x: float
    y: float
    degree: int


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str


class GraphResponse(BaseModel):
    """The foreign-key graph, laid out and ready to draw.

    Positions are computed server-side with NetworkX rather than in the
    browser: the layout algorithm is already a dependency here, and it keeps
    the client a pure renderer.
    """

    nodes: list[GraphNode]
    edges: list[GraphEdge]


class DatasetResponse(BaseModel):
    id: str
    name: str
    kind: str
    tables: list[str]
    row_counts: dict[str, int]
    created_at: str
    note: str


class SearchHit(BaseModel):
    """One past turn, plus enough context to jump to where it was asked."""

    id: int
    asked_at: str
    question: str
    sql: str | None
    ok: bool
    row_count: int | None
    conversation_id: str | None
    conversation_title: str | None


class ToggleInfo(BaseModel):
    name: str
    label: str
    help: str
    cost: str
    kind: Literal["switch", "choice"]
    choices: list[str] = []

    numeric: bool = False
    """Whether the choices are numbers rather than string literals.

    Declared by the server because the client was deciding it from a hard-coded
    list of names. That list is the same failure as any other hand-maintained
    parallel list: a new numeric toggle sends ``"4"`` where the schema wants
    ``4``, and the user gets a 422 they cannot act on.
    """


class OptionsResponse(BaseModel):
    defaults: dict[str, Any]
    toggles: list[ToggleInfo]


class SuggestionsResponse(BaseModel):
    """Opening questions for an empty screen.

    Empty is a valid answer: the model call is decoration on an otherwise
    working interface, and the client falls back to schema-derived suggestions
    rather than showing nothing.
    """

    questions: list[str]


class StatsResponse(BaseModel):
    total: int
    successful: int
    clarified: int
    """Turns where the agent asked a question back instead of answering.

    Reported separately and excluded from ``success_rate``. Counted as failures
    it read as a collapse: 65% answered on a system where almost nothing had
    failed and a third of turns were the agent declining to guess."""

    success_rate: float
    mean_seconds: float
    total_tokens: int


class DriftShift(BaseModel):
    metric: str
    baseline: float
    recent: float
    change: float
    worse: bool
    p_value: float
    description: str


class DriftResponse(BaseModel):
    """Whether the last N questions went differently from the ones before them.

    Reports behaviour, never correctness. Nothing here knows whether an answer
    was right — there is no label — so a shift is a prompt to go and look, not
    a verdict.
    """

    baseline_n: int
    recent_n: int
    enough_data: bool
    """False when there is not enough history to compare. Distinct from an
    empty ``shifts``: one means nothing moved, the other means nobody looked."""

    drifted: bool
    shifts: list[DriftShift]
    rates: dict[str, list[float]]
    summary: str


TOGGLE_DESCRIPTIONS: list[ToggleInfo] = [
    ToggleInfo(
        name="initial_hops",
        label="How much schema to show",
        help=(
            "The agent starts from the tables your question names and walks "
            "foreign keys outward. 1 hop is those tables plus whatever they "
            "join to directly; 2 goes a step further. More tables means the "
            "agent is less likely to miss one it needed, and more likely to be "
            "distracted by one it did not. The tables it actually used are "
            "listed under every answer."
        ),
        cost="more tables = a larger prompt, more tokens",
        kind="choice",
        choices=["1", "2", "3"],
        numeric=True,
    ),
    ToggleInfo(
        name="quality_tier",
        label="Care",
        help=(
            "Fast answers most questions correctly in one go. Medium writes "
            "the query three times and keeps the version that recurs, which "
            "helps where the model is guessing. Detailed adds a second model "
            "reviewing the query and splits a multi-part question into parts — "
            "for a figure that will be acted on rather than glanced at."
        ),
        cost="medium ~2x the tokens; detailed ~4x and several times slower",
        kind="choice",
        choices=["fast", "medium", "thorough"],
    ),
    ToggleInfo(
        name="use_critic",
        label="Review the query",
        help=(
            "A second model checks the SQL actually answers the question before "
            "it runs. Catches queries that are safe, cheap and about the wrong "
            "thing — the failure no mechanical check can see."
        ),
        cost="+1 model call",
        kind="switch",
    ),
    ToggleInfo(
        name="vote_samples",
        label="Write it N times",
        help=(
            "Generate the query several times and keep the one that recurs. "
            "Where the model is confident the samples agree and this changes "
            "nothing; where it is guessing, the version that repeats is more "
            "often right."
        ),
        cost="N model calls instead of 1",
        kind="choice",
        choices=["1", "3", "5"],
        numeric=True,
    ),
    ToggleInfo(
        name="ambiguity_handling",
        label="Ask when unclear",
        help=(
            "Some questions have more than one defensible answer — 'top "
            "customers' by revenue or by order count? This asks instead of "
            "picking one silently."
        ),
        cost="+1 model call, and a question back to you",
        kind="choice",
        choices=["best_effort", "ask_human"],
    ),
    ToggleInfo(
        name="decompose_questions",
        label="Split multi-part questions",
        help=(
            "A question that is really several questions gets split, each part "
            "answered separately, and the findings written up as one answer."
        ),
        cost="+1 call, then a full question per part",
        kind="switch",
    ),
    ToggleInfo(
        name="prescreen_input",
        label="Screen the question",
        help=(
            "Checks the question before acting on it. Defends the conversation, "
            "not the database — the read-only role and the SQL validator already "
            "make writes impossible either way."
        ),
        cost="+1 model call",
        kind="switch",
    ),
    ToggleInfo(
        name="summarise_conversation",
        label="Remember the whole chat",
        help=(
            "The agent reads the last few turns in full. Older ones are folded "
            "into a short note of what still applies — the filters you set, "
            "what you are looking at — so a constraint from ten questions ago "
            "does not quietly stop applying."
        ),
        cost="+1 model call, only once a chat outgrows the window",
        kind="switch",
    ),
    ToggleInfo(
        name="max_clarifying_questions",
        label="Questions asked back",
        help=(
            "When a question has more than one defensible answer, the agent "
            "asks rather than guessing. This caps how many things it asks "
            "about at once — it asks about as many ambiguities as the question "
            "actually has, up to this. Lower it for a terse exchange; the "
            "agent will ask about the worst one and take its best guess at the "
            "rest."
        ),
        cost="a longer exchange before any answer",
        kind="choice",
        choices=["1", "3", "5", "7"],
        numeric=True,
    ),
    ToggleInfo(
        name="conversation_window",
        label="Turns kept in full",
        help=(
            "How many recent exchanges the agent sees verbatim, with their SQL "
            "and results. More is not better: the transcript competes with the "
            "schema for room in the prompt, and the schema is what makes the "
            "next query correct."
        ),
        cost="a larger prompt on every follow-up",
        kind="choice",
        choices=["2", "4", "8"],
        numeric=True,
    ),
    ToggleInfo(
        name="cache_sql",
        label="Reuse queries",
        help=(
            "An identical question reuses the SQL it produced before, skipping "
            "generation. The query still runs against live data, so the answer "
            "is current — only the writing of it is reused."
        ),
        cost="saves ~2 model calls on a repeat",
        kind="switch",
    ),
]
"""What each toggle is, in the user's terms, with its cost stated.

The cost is not decoration. A toggle offered without one invites someone to
switch everything on and conclude the tool is slow.
"""


class NewConversationRequest(BaseModel):
    dataset_id: str | None = None


class ConversationSummary(BaseModel):
    id: str
    started_at: str
    updated_at: str
    title: str
    dataset_id: str | None
    turns: int


class ConversationDetail(BaseModel):
    """One thread plus its turns, in the order they were asked."""

    conversation: ConversationSummary
    entries: list[dict]


class HealthResponse(BaseModel):
    status: str
    tables: int
    schema_version: str
    light_model: str
    strong_model: str

    auth_required: bool = False
    """Whether every request must resolve to a tenant.

    Reported because "is this deployment open?" must be answerable without
    reading the environment of a running container. A hosted instance that
    quietly came up with authentication off looks identical to one that did
    not, from the outside, until someone notices — and the whole point of a
    health endpoint is that a machine can check.
    """

    encryption_configured: bool = False
    """Whether a key is loaded for tenant credentials. Never the key, and never
    how many — only that there is one, which is what a readiness probe needs to
    refuse traffic to an instance that cannot decrypt anything."""


def to_response(result: AgentResult, conversation_id: str | None = None) -> AskResponse:
    query = result.result
    return AskResponse(
        question=result.question,
        answer=result.answer,
        conversation_id=conversation_id,
        sql=result.sql,
        columns=list(query.columns) if query else [],
        rows=[list(row) for row in query.rows] if query else [],
        row_count=query.row_count if query else 0,
        truncated=query.truncated if query else False,
        ok=result.ok,
        error=result.error,
        repairs=result.trace.repair_count,
        model_calls=result.trace.model_calls,
        input_tokens=result.trace.input_tokens,
        output_tokens=result.trace.output_tokens,
        seconds=result.trace.seconds,
        tables_considered=result.trace.candidate_tables,
        clarification_asks=result.clarification_asks,
        warnings=_warnings(result),
    )


def _warnings(result: AgentResult) -> list[str]:
    """Everything the guards flagged without failing the request.

    Phrased for someone reading a figure, not for someone reading a stack trace:
    the point is to make them look at the query, so the message has to say what
    might be wrong with the *number*.
    """
    warnings = list(result.trace.inflation_warnings)

    if result.trace.answer_unverified:
        warnings.append(
            "The written answer could not be reconciled with the rows returned - "
            "read the result table rather than the sentence."
        )

    return warnings


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_agent() -> SqlAgent:
    """Build the agent once and reuse it.

    Cached because construction reflects the whole schema and builds the graph.
    Doing that per request would add seconds to every question for no benefit —
    the schema does not change between two questions asked a second apart.
    """
    config: Settings = settings()
    engine = create_engine(config.database_url, pool_pre_ping=True)
    agent = SqlAgent(engine, config=config)
    agent.warm()
    return agent


@lru_cache(maxsize=1)
def get_store() -> Store:
    """History and dataset records. One SQLite file under the data directory."""
    return Store(Path(settings().data_dir) / "sqlagent.db")


@lru_cache(maxsize=8)
def get_dataset_agent(dataset_id: str) -> SqlAgent:
    """An agent bound to one uploaded dataset.

    Cached per dataset, for the same reason the main agent is: constructing one
    reflects the whole schema. Bounded at 8 so a server with many uploads does
    not hold every schema in memory forever.
    """
    record = get_store().get_dataset(dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No dataset '{dataset_id}'")

    config = settings()
    engine = create_engine(record["database_url"], pool_pre_ping=True)
    agent = SqlAgent(engine, config=config)
    agent.warm()
    return agent


def resolve_agent(dataset_id: str | None) -> SqlAgent:
    return get_dataset_agent(dataset_id) if dataset_id else get_agent()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    Reflecting the schema at startup means the first user does not pay for it.
    If the database is unreachable the server still starts, and ``/api/health``
    reports the problem — better than refusing to boot, which in a container
    turns into a crash loop with the real error buried in logs.
    """
    try:
        await run_in_threadpool(get_agent)
        logger.info("agent ready")
    except Exception:  # noqa: BLE001
        logger.exception("could not prepare the agent at startup")
    yield


# `Annotated[X, Depends(f)]` is the current FastAPI idiom for injection: it
# keeps the dependency in the type rather than in a mutable default argument,
# which is both clearer and avoids the classic Python default-argument trap.
AgentDep = Annotated[SqlAgent, Depends(get_agent)]


app = FastAPI(
    title="SQL Agent",
    description="Ask questions about a database in plain language.",
    version="0.1.0",
    lifespan=lifespan,
)

# The dev frontend runs on a different port, which makes every request
# cross-origin. Tightened to specific origins rather than "*" — a wildcard here
# would let any site a user visits call this API from their browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    # Credentials are allowed because the browser sends a session cookie. That
    # is exactly why `allow_origins` above cannot become "*": the two together
    # are forbidden by the spec, and a wildcard with credentials would let any
    # site a signed-in user visits call this API as them.
    allow_credentials=True,
    # DELETE is needed for revoking keys, conversations and connections.
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Sign-up, sign-in, sign-out, whoami and usage. Mounted after CORS so those
# routes get the same headers as everything else — a login that works in curl
# and fails in a browser is the most confusing possible version of this bug.
app.include_router(auth_router)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/api/health", response_model=HealthResponse)
def health(agent: AgentDep) -> HealthResponse:
    config = settings()
    return HealthResponse(
        status="ok",
        tables=len(agent.snapshot),
        schema_version=agent.snapshot.version,
        light_model=config.light_model,
        strong_model=config.strong_model,
        auth_required=config.require_auth,
        encryption_configured=bool(config.secret_key),
    )


@app.get("/api/schema", response_model=SchemaResponse)
def get_schema(agent: AgentDep, principal: PrincipalDep) -> SchemaResponse:
    """The database structure, for the UI's schema browser."""
    snapshot = agent.snapshot
    tables = [
        TableInfo(
            name=table.name,
            columns=[f"{column.name} {column.type}" for column in table.columns],
            primary_key=list(table.primary_key),
            references=sorted({fk.target_table for fk in table.foreign_keys}),
        )
        for table in sorted(snapshot.tables.values(), key=lambda t: t.name)
    ]
    return SchemaResponse(
        version=snapshot.version, table_count=len(tables), tables=tables
    )


@app.post("/api/ask", response_model=AskResponse)
async def ask(request: AskRequest, principal: PrincipalDep) -> AskResponse:
    """Answer a question and return everything at once."""
    agent = await run_in_threadpool(resolve_agent, request.dataset_id)
    conversation_id = await run_in_threadpool(
        _ensure_conversation, request.conversation_id, request.dataset_id
    )
    history = await run_in_threadpool(_load_history, conversation_id)

    # Quota before any work, clamping included. Raises 402 when the allowance
    # is spent; a tier the plan does not include is quietly reduced rather than
    # refused, because turning one over-ambitious field into an outage is a bad
    # trade for an upsell.
    options = await run_in_threadpool(
        enforce_and_clamp,
        principal,
        request.options.overrides() if request.options else None,
    )

    result = await run_in_threadpool(
        agent.ask, request.question, history=history, options=options
    )

    # Counted after, not before. A question that failed on a model timeout must
    # not consume an allowance — a customer charged for an error writes a
    # support ticket that costs more than the question did.
    if result.ok:
        await run_in_threadpool(record_question, principal, options)

    await run_in_threadpool(_record, result, request.dataset_id, conversation_id)
    return to_response(result, conversation_id)


def _ensure_conversation(conversation_id: str | None, dataset_id: str | None) -> str:
    """Return the thread to use, creating one if the client did not name it.

    The server creates the thread rather than requiring the client to do it
    first. That removes a round trip, and removes the failure mode where the
    create succeeds, the ask fails, and an empty thread is left behind.

    An unknown id is treated as a request for a new thread rather than a 404. A
    stale id in a browser tab — from a cleared store, or a different machine —
    should cost the user nothing.
    """
    store = get_store()
    if conversation_id and store.get_conversation(conversation_id):
        return conversation_id
    return store.create_conversation(dataset_id=dataset_id)


def _load_history(conversation_id: str | None) -> list[Turn]:
    """Read a thread's turns for the model.

    Only *successful* turns carry SQL worth building on, but failed ones are
    kept too and marked: a model that can see its last attempt failed will not
    reproduce it.

    Best-effort. If the store cannot be read the question is still answerable —
    just without context — and that is strictly better than refusing to answer.
    """
    if not conversation_id:
        return []
    try:
        return [
            Turn(
                question=entry.question,
                sql=entry.sql,
                ok=entry.ok,
                # The stored preview holds up to `stored_result_rows` for
                # display. The model gets a far tighter bound applied here, so
                # widening what is *shown* never widens what is *prompted*.
                **_carried(entry.result_preview),
                row_count=entry.row_count,
                # A turn that asked something is owed a reply, and the next
                # message is it. Without this the turn reads as a failure.
                clarification=(
                    entry.answer if entry.error == CLARIFICATION_ERROR else None
                ),
            )
            for entry in get_store().conversation_entries(conversation_id)
        ]
    except Exception:  # noqa: BLE001 - context is an enhancement, not a requirement
        logger.exception("could not load conversation history")
        return []


def _carried(preview: dict | None) -> dict:
    """Re-bound a stored display preview to what the model may see.

    The store keeps up to fifty rows so a reopened conversation still shows its
    table. The prompt gets at most three, and only when the whole result was
    that small — see ``conversation.carryable_result``. Applying the tighter
    bound here means the display limit can be raised without ever widening what
    reaches a prompt.
    """
    if not preview:
        return {"result_columns": (), "result_rows": ()}

    # A stored preview that was itself truncated is not the whole result, so it
    # can never be carried: the model would treat a sample as complete.
    if preview.get("truncated"):
        return {"result_columns": (), "result_rows": ()}

    columns, rows = carryable_result(
        preview.get("columns", []), preview.get("rows", [])
    )
    return {"result_columns": columns, "result_rows": rows}


def _jsonable(cell: object) -> object:
    """Make one result cell safe to store as JSON.

    Dates, Decimals and UUIDs all arrive as driver objects that ``json.dumps``
    rejects. They are rendered as text, which is also the form the model will be
    shown, so nothing is lost by converting here.
    """
    if cell is None or isinstance(cell, bool | int | float | str):
        return cell
    return str(cell)


def _record(
    result: AgentResult, dataset_id: str | None, conversation_id: str | None = None
) -> None:
    """Append one question to the history.

    Best-effort: a store that cannot be written must not fail a request that
    otherwise succeeded. Result *rows* are deliberately not stored — a query
    like "list every customer" would turn the log into a shadow copy of the
    database.
    """
    try:
        store = get_store()

        # A bounded display preview, so reopening a conversation still shows its
        # tables. Bounded, not unbounded: "list every customer" stores the first
        # `stored_result_rows` and records that it was truncated, rather than
        # turning the log into a copy of the database.
        #
        # The model sees far less than this — `carryable_result` re-bounds it to
        # three rows when the turn is loaded as context.
        preview = None
        limit = settings().stored_result_rows
        if result.result is not None and limit > 0 and result.result.rows:
            kept = result.result.rows[:limit]
            preview = {
                "columns": list(result.result.columns),
                "rows": [[_jsonable(cell) for cell in row] for row in kept],
                "row_count": result.result.row_count,
                "truncated": result.result.row_count > len(kept),
            }

        store.record_question(
            result_preview=preview,
            question=result.question,
            answer=result.answer,
            sql=result.sql,
            ok=result.ok,
            error=result.error,
            dataset_id=dataset_id,
            conversation_id=conversation_id,
            row_count=result.result.row_count if result.result else None,
            seconds=result.trace.seconds,
            model_calls=result.trace.model_calls,
            tokens=result.trace.input_tokens + result.trace.output_tokens,
            repairs=result.trace.repair_count,
        )
        if conversation_id:
            store.touch_conversation(conversation_id, title=result.question)
    except Exception:  # noqa: BLE001 - history must never break answering
        logger.exception("could not record history")


@app.get("/api/ask/stream")
async def ask_stream(
    principal: PrincipalDep,
    question: Annotated[str, Query(min_length=1, max_length=2000)],
    dataset_id: Annotated[str | None, Query()] = None,
    conversation_id: Annotated[str | None, Query()] = None,
    # EventSource can only issue a GET, so the options arrive as a JSON string
    # in the query rather than as a body. Parsed and validated through the same
    # model the POST uses, so the two routes cannot diverge on what is settable.
    options: Annotated[str | None, Query(max_length=500)] = None,
) -> StreamingResponse:
    """Answer a question, streaming progress as Server-Sent Events.

    A GET with the question in the query string, because ``EventSource`` — the
    browser API for SSE — can only issue GETs.

    The pipeline is synchronous, so it runs in a worker thread and pushes events
    onto a queue that this coroutine drains. That is the standard way to bridge
    blocking code into an async stream without blocking the event loop.
    """
    if not question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    try:
        parsed_options = (
            AskOptions.model_validate_json(options).overrides() if options else None
        )
    except Exception as exc:  # noqa: BLE001 - a bad query string is a 400, not a 500
        raise HTTPException(
            status_code=400, detail=f"invalid options: {exc}"
        ) from exc

    # The same enforcement as /api/ask. Two handlers answering questions means
    # two places a quota can be forgotten, so both call the one function and a
    # test asserts neither passes raw options to the agent.
    parsed_options = await run_in_threadpool(
        enforce_and_clamp, principal, parsed_options
    )

    agent = await run_in_threadpool(resolve_agent, dataset_id)
    thread_id = await run_in_threadpool(_ensure_conversation, conversation_id, dataset_id)
    history = await run_in_threadpool(_load_history, thread_id)

    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_progress(stage: str, detail: dict) -> None:
        # Called from the worker thread, so hand the item to the event loop
        # thread-safely rather than touching the queue directly.
        loop.call_soon_threadsafe(queue.put_nowait, (stage, detail))

    async def run() -> None:
        try:
            result = await run_in_threadpool(
                agent.ask,
                question,
                history=history,
                options=parsed_options,
                on_progress=on_progress,
            )
            if result.ok:
                await run_in_threadpool(record_question, principal, parsed_options)
            await run_in_threadpool(_record, result, dataset_id, thread_id)
            await queue.put(("result", to_response(result, thread_id).model_dump()))
        except Exception as exc:  # noqa: BLE001
            logger.exception("streaming request failed")
            await queue.put(("error", {"message": str(exc)}))
        finally:
            await queue.put(None)  # sentinel: the stream is finished

    async def events() -> AsyncIterator[str]:
        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                stage, detail = item
                # SSE frame format: an event name, a data line, then a blank
                # line that terminates the frame.
                yield f"event: {stage}\ndata: {json.dumps(detail, default=str)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tells nginx not to buffer the response, which would defeat
            # streaming entirely by holding events until the request finished.
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------
# Schema graph
# --------------------------------------------------------------------------


@app.get("/api/schema/graph", response_model=GraphResponse)
def schema_graph(
    principal: PrincipalDep,
    dataset_id: Annotated[str | None, Query()] = None,
) -> GraphResponse:
    """The foreign-key graph, with positions, ready for the client to draw.

    Layout is computed here rather than in the browser: NetworkX is already a
    dependency, the graph is small, and it keeps the client a pure renderer
    with no layout engine of its own.
    """
    agent = resolve_agent(dataset_id)
    graph = agent.graph
    snapshot = agent.snapshot

    if len(graph) == 0:
        return GraphResponse(nodes=[], edges=[])

    positions = _layout(graph)

    # Scale the unit-ish coordinates NetworkX produces (roughly -1..1) into
    # pixels.
    #
    # Spread grows with the *square root* of node count, not linearly. Nodes
    # tile a 2D area, so area scales with n and side length with sqrt(n).
    # Scaling linearly — as a first attempt did — makes a 75-table graph
    # roughly twice as wide as it needs to be, and `fitView` then zooms out
    # far enough that every label becomes an illegible dash.
    spread = round(60 * math.sqrt(len(graph)) + 90)

    nodes = [
        GraphNode(
            id=str(name),
            label=str(name),
            columns=len(snapshot.tables[name].columns) if name in snapshot else 0,
            x=round(float(position[0]) * spread, 1),
            y=round(float(position[1]) * spread, 1),
            degree=graph.degree(name),
        )
        for name, position in sorted(positions.items())
    ]

    edges = []
    for source, target, data in graph.edges(data=True):
        keys = data.get(EDGE_FOREIGN_KEY, [])
        # Several foreign keys can join the same pair of tables; show how many
        # rather than drawing overlapping edges.
        label = keys[0].join_condition() if len(keys) == 1 else f"{len(keys)} keys"
        edges.append(
            GraphEdge(
                id=f"{source}->{target}",
                source=str(source),
                target=str(target),
                label=label,
            )
        )

    return GraphResponse(nodes=nodes, edges=edges)


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------


@app.post("/api/datasets", response_model=DatasetResponse)
async def upload_dataset(
    file: Annotated[UploadFile, File()], principal: PrincipalDep
) -> DatasetResponse:
    """Upload a CSV, Excel workbook or PostgreSQL dump and make it queryable.

    The file is streamed to a temporary path first: reading a 500 MB upload
    into memory to inspect it would be a simple way to exhaust the server.
    """
    config = settings()
    original = file.filename or "upload"

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(original).suffix) as scratch:
        temporary = Path(scratch.name)
        await run_in_threadpool(shutil.copyfileobj, file.file, scratch)

    try:
        dataset = await run_in_threadpool(
            ingest_file,
            temporary,
            data_dir=Path(config.data_dir) / "datasets",
            original_name=original,
            postgres_admin_url=config.postgres_admin_url or None,
        )
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)

    await run_in_threadpool(get_store().save_dataset, dataset)
    return DatasetResponse(**dataset.to_dict())


@app.get("/api/datasets", response_model=list[DatasetResponse])
def list_datasets(principal: PrincipalDep) -> list[DatasetResponse]:
    return [
        DatasetResponse(
            id=d["id"], name=d["name"], kind=d["kind"], tables=d["tables"],
            row_counts=d["row_counts"], created_at=d["created_at"], note=d["note"] or "",
        )
        for d in get_store().list_datasets()
    ]


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, principal: PrincipalDep) -> dict:
    if not get_store().delete_dataset(dataset_id):
        raise HTTPException(status_code=404, detail=f"No dataset '{dataset_id}'")
    get_dataset_agent.cache_clear()
    return {"deleted": dataset_id}


# --------------------------------------------------------------------------
# Conversations
#
# History answers "what have I asked?"; a conversation answers "what were we
# talking about?". They share a table — a conversation is a group of history
# entries — but they are different views, and the UI shows them separately.
# --------------------------------------------------------------------------


@app.get("/api/conversations", response_model=list[ConversationSummary])
def list_conversations(
    principal: PrincipalDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[dict]:
    """Threads, most recently used first."""
    return get_store().list_conversations(limit=limit)


@app.post("/api/conversations", response_model=ConversationSummary)
def create_conversation(request: NewConversationRequest, principal: PrincipalDep) -> dict:
    """Start an empty thread.

    Rarely needed — asking a question with no ``conversation_id`` creates one
    implicitly. It exists so the UI's "New chat" button can clear the screen
    immediately, without waiting for a question to be typed.
    """
    store = get_store()
    conversation_id = store.create_conversation(dataset_id=request.dataset_id)
    created = store.get_conversation(conversation_id)
    assert created is not None
    return {**created, "turns": 0}


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, principal: PrincipalDep) -> ConversationDetail:
    """One thread and every turn in it, oldest first.

    This is what the UI calls to restore a thread after a reload, so the
    transcript survives a refresh rather than living only in browser memory.
    """
    store = get_store()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"No conversation '{conversation_id}'")

    entries = store.conversation_entries(conversation_id)
    return ConversationDetail(
        conversation={**conversation, "turns": len(entries)},
        entries=[entry.to_dict() for entry in entries],
    )


@app.get("/api/search", response_model=list[SearchHit])
def search_turns(
    principal: PrincipalDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SearchHit]:
    """Find past questions, and say which conversation each came from.

    This is what is left of the flat history view, and the only part of it worth
    keeping. A bare list of every question ever asked is mostly unreadable —
    half the entries are fragments like "and for April?" that have no subject
    outside their thread. A search result that names its conversation can be
    opened in context, which makes it useful again.
    """
    store = get_store()
    titles = {item["id"]: item["title"] for item in store.list_conversations(limit=200)}

    return [
        SearchHit(
            id=entry.id,
            asked_at=entry.asked_at,
            question=entry.question,
            sql=entry.sql,
            ok=entry.ok,
            row_count=entry.row_count,
            conversation_id=entry.conversation_id,
            conversation_title=titles.get(entry.conversation_id or ""),
        )
        for entry in store.search_turns(q, limit=limit)
    ]


@app.get("/api/options", response_model=OptionsResponse)
def describe_options() -> OptionsResponse:
    """The toggles, their current server defaults, and what each costs.

    Served rather than hardcoded in the client for two reasons: the defaults are
    a server decision that a deployment may change, and the explanations belong
    next to the code that implements them. A user turning on "thorough" deserves
    to be told it means three generations plus a review, not to discover it in
    the latency.
    """
    config = settings()
    return OptionsResponse(
        defaults={name: getattr(config, name) for name in sorted(OVERRIDABLE)},
        toggles=TOGGLE_DESCRIPTIONS,
    )


_SUGGESTIONS: dict[str, list[str]] = {}
"""Proposed questions, keyed by schema version.

A plain dict rather than ``lru_cache``: the value is computed *from the agent*,
and threading an agent through a hashable cache key needs an indirection that
is more machinery than a four-line dictionary.

Unbounded in principle, bounded in practice by how many distinct schemas one
process ever sees — one, plus an entry per uploaded dataset.
"""


@app.get("/api/suggestions", response_model=SuggestionsResponse)
def suggestions(agent: AgentDep, principal: PrincipalDep) -> SuggestionsResponse:
    """Opening questions, proposed once per schema version.

    Keyed on the schema hash, which already changes on exactly the events that
    should invalidate them — a column rename refreshes the suggestions and
    nothing else does.
    """
    from sqlagent.suggest import propose

    version = agent.snapshot.version
    if version not in _SUGGESTIONS:
        _SUGGESTIONS[version] = propose(
            agent.client,
            tables=sorted(agent.snapshot.tables),
            glossary=agent.glossary.render(),
            model=agent.config.light_model,
        )
    return SuggestionsResponse(questions=_SUGGESTIONS[version])


@app.get("/api/stats", response_model=StatsResponse)
def stats(principal: PrincipalDep) -> dict:
    """Aggregate numbers across every question ever asked."""
    return get_store().stats()


class PlanResponse(BaseModel):
    """One tier, as the pricing page needs it.

    Served rather than hardcoded in the client. The numbers are pricing policy
    and they live in `saas/plans.py`; a second copy in a React component is a
    copy that will disagree with billing on the day one of them changes.
    """

    name: str
    label: str
    price_monthly_usd: int
    price_monthly_inr: int
    custom_priced: bool
    """True means "talk to us", not "free". Zero means both in the table, and
    the difference must not be left to whoever writes the template."""

    questions_per_month: int
    detailed_per_month: int
    max_quality_tier: str
    strong_model: bool
    max_connected_databases: int
    max_uploaded_datasets: int
    max_seats: int
    row_limit: int
    history_retention_days: int
    features: list[str]


@app.get("/api/plans", response_model=list[PlanResponse])
def plans() -> list[PlanResponse]:
    """The tiers. Public: a pricing page is read before anyone has an account."""
    from sqlagent.saas.plans import CUSTOM_PRICED, PLANS

    return [
        PlanResponse(
            name=plan.name,
            label=plan.label,
            price_monthly_usd=plan.price_monthly_usd,
            price_monthly_inr=plan.price_monthly_inr,
            custom_priced=plan.name in CUSTOM_PRICED,
            questions_per_month=plan.questions_per_month,
            detailed_per_month=plan.detailed_per_month,
            max_quality_tier=plan.max_quality_tier,
            strong_model=plan.strong_model,
            max_connected_databases=plan.max_connected_databases,
            max_uploaded_datasets=plan.max_uploaded_datasets,
            max_seats=plan.max_seats,
            row_limit=plan.row_limit,
            history_retention_days=plan.history_retention_days,
            features=sorted(plan.features),
        )
        for plan in PLANS.values()
    ]


@app.get("/api/drift", response_model=DriftResponse)
def drift(
    principal: PrincipalDep,
    recent: Annotated[int, Query(ge=10, le=2000)] = 100,
    baseline: Annotated[int, Query(ge=10, le=5000)] = 300,
) -> DriftResponse:
    """Compare the last ``recent`` questions against the ``baseline`` before them.

    Both windows are counts of questions rather than spans of time. A week is
    not a comparable unit here — one week may hold four hundred questions and
    the next eleven — and a rate computed over eleven questions is not a rate.
    """
    from sqlagent.drift import detect_drift, split_history

    entries = get_store().recent_turns(limit=recent + baseline)
    earlier, latest = split_history(entries, recent_n=recent)
    report = detect_drift(earlier, latest)

    return DriftResponse(
        baseline_n=report.baseline_n,
        recent_n=report.recent_n,
        enough_data=report.enough_data,
        drifted=report.drifted,
        shifts=[
            DriftShift(
                metric=shift.metric,
                baseline=shift.baseline,
                recent=shift.recent,
                change=shift.change,
                worse=shift.worse,
                p_value=shift.p_value,
                description=shift.render(),
            )
            for shift in report.shifts
        ],
        rates={name: list(pair) for name, pair in report.rates.items()},
        summary=report.render(),
    )


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, principal: PrincipalDep) -> dict:
    if not get_store().delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail=f"No conversation '{conversation_id}'")
    return {"deleted": conversation_id}


def _layout(graph) -> dict:
    """Position every table, degrading gracefully rather than failing.

    Three algorithms, best first:

    1. ``kamada_kawai`` — places related tables close together, which is the
       structure actually worth looking at. Needs SciPy, and is O(n^2).
    2. ``spring_layout`` — a decent force-directed approximation, needs only
       NumPy. Seeded so the picture does not rearrange on every refresh.
    3. ``circular_layout`` — pure Python, always works, and at least shows
       every table and every edge.

    Falling back rather than raising matters: a missing optional dependency
    should cost picture quality, not the whole feature.
    """
    import networkx as nx

    undirected = graph.to_undirected()

    if len(graph) == 1:
        return {next(iter(graph.nodes)): (0.0, 0.0)}

    if len(graph) <= 60:
        try:
            return nx.kamada_kawai_layout(undirected)
        except (ImportError, ModuleNotFoundError):
            logger.debug("SciPy absent; falling back to a spring layout")

    try:
        return nx.spring_layout(undirected, seed=7, k=0.9)
    except (ImportError, ModuleNotFoundError):
        logger.debug("NumPy absent; falling back to a circular layout")
        return nx.circular_layout(undirected)
