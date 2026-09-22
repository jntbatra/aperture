# 4. Code walkthrough

Module by module, in dependency order — leaves first, then the pipeline that
uses them. For each: what it does, the key code, and the non-obvious decisions.

---

## `schema/introspect.py` — reading the database

### What it produces

```python
@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    tables: dict[str, Table]
    version: str            # fingerprint of the structure
```

Everything is frozen, because a snapshot is built once and shared across every
request. A mutation in one request would corrupt all later ones.

### Reflection

```python
metadata = MetaData(schema=schema)
metadata.reflect(bind=engine)

for table_name in sorted(metadata.tables):
    ...
```

`sorted` is not cosmetic. The version hash is computed from this iteration, so
an unsorted order would produce a different hash run to run and invalidate
every cache at random.

### Composite foreign keys — the subtle one

```python
foreign_keys = tuple(sorted(
    (
        ForeignKey(
            source_table=_unqualified(constraint.table.fullname),
            source_columns=tuple(col.name for col in constraint.columns),
            target_table=_unqualified(constraint.referred_table.fullname),
            target_columns=tuple(e.column.name for e in constraint.elements),
        )
        for constraint in sa_table.foreign_key_constraints   # ← not .foreign_keys
    ),
    key=lambda fk: (fk.target_table, fk.source_columns),
))
```

`sa_table.foreign_keys` yields one entry **per column**. A composite key —
`FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id)` — would
become two unrelated single-column links, and a query joining on half of it
returns wrong rows with no error at all.

`foreign_key_constraints` keeps each constraint whole, which is why
`join_condition()` can render every column pair:

```python
def join_condition(self, source_alias=None, target_alias=None) -> str:
    pairs = [
        f"{left}.{src} = {right}.{tgt}"
        for src, tgt in zip(self.source_columns, self.target_columns, strict=True)
    ]
    return " AND ".join(pairs)
```

`strict=True` makes a mismatched key raise rather than silently truncate to the
shorter list — a malformed constraint should be loud.

### The version hash

```python
def compute_version(tables) -> str:
    canonical = [...]                       # names, types, nullability, PKs, FKs
    payload = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
```

Included: structure. Excluded: row counts and statistics, which change
constantly and must not invalidate a cache that depends only on shape.

---

## `schema/graph.py` — tables as a graph

```python
def build_graph(snapshot: SchemaSnapshot) -> nx.DiGraph:
    graph = nx.DiGraph()

    for table_name in snapshot.tables:
        graph.add_node(table_name)          # isolated tables too

    for fk in snapshot.foreign_keys:
        if graph.has_edge(fk.source_table, fk.target_table):
            graph[fk.source_table][fk.target_table][EDGE_FOREIGN_KEY].append(fk)
        else:
            graph.add_edge(fk.source_table, fk.target_table, **{EDGE_FOREIGN_KEY: [fk]})
```

Three decisions in fifteen lines:

**Every table becomes a node, including ones with no foreign keys.** A table
that never became a node could never be selected, and plenty of useful
questions concern a single standalone table.

**Edge direction is child → parent**, matching how the constraint is declared.

**The edge attribute is a *list*.** A `DiGraph` stores one edge per node pair,
so two foreign keys between the same pair — an order with both a
`billing_address_id` and a `shipping_address_id` pointing at `addresses` —
would overwrite each other. Keeping a list preserves both join paths. There is
a test for exactly this.

### Rendering joins for the prompt

```python
def describe_edges(graph, tables: set[str]) -> list[str]:
    predicates = set()
    for source, target, data in graph.edges(data=True):
        if source in tables and target in tables:
            for fk in data[EDGE_FOREIGN_KEY]:
                predicates.add(fk.join_condition())
    return sorted(predicates)
```

Only edges with **both** endpoints in the selection: a join to a table the
model was not shown is not actionable. Sorted so the prompt is byte-identical
across runs, which caching and reproducible evaluation both depend on.

---

## `schema/retrieval.py` — picking the relevant corner

```python
def expand(graph, seeds, *, max_hops=DEFAULT_MAX_HOPS) -> Neighbourhood:
    distances: dict[str, int] = {seed: 0 for seed in seeds}
    queue: deque[str] = deque(seeds)

    while queue:
        current = queue.popleft()
        if distances[current] >= hop_limit:
            continue
        for neighbour in _adjacent(graph, current):
            if neighbour not in distances:
                distances[neighbour] = distances[current] + 1
                queue.append(neighbour)
```

`distances` doubles as the visited set. That is what makes cycles safe: a
mutual foreign key finds its target already recorded and stops. Remove it and
the mutual-reference test *hangs* rather than failing.

### Both directions

```python
def _adjacent(graph, table) -> set[str]:
    neighbours = set(graph.successors(table)) | set(graph.predecessors(table))
    neighbours.discard(table)          # a self-loop is not a neighbour
    return neighbours
```

`successors` are the tables this one references; `predecessors` are the tables
referencing it. Relevance runs both ways.

This is the single most important line in the module. Following only outgoing
edges from `customers` finds nothing — it is a parent and declares no keys —
and `orders`, the most relevant table for almost any customer question, would
never be retrieved.

`discard(table)` handles `employees.manager_id → employees.id`, which would
otherwise report the table as its own neighbour at distance 1.

### Ordering

```python
def ordered(self) -> list[str]:
    return sorted(self.distances, key=lambda t: (self.distances[t], t))
```

Nearest first (models weight earlier context more heavily), then alphabetical
so output is deterministic.

---

## `guards/validator.py` — the security boundary

```python
def validate(sql, *, dialect="postgres", row_limit=None, allowed_tables=None) -> ValidationResult:
    statements = _parse(sql, dialect)
    if len(statements) > 1:
        raise ValidationError(f"Expected exactly one statement, found {len(statements)}...")

    statement = statements[0]
    _reject_non_select(statement)
    _reject_forbidden_functions(statement)
    _reject_data_modifying_ctes(statement)
    ...
```

### Allow-list, not deny-list

```python
ALLOWED_STATEMENTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)
```

Anything not on this list is refused. If a future sqlglot version introduces a
new expression type, it is blocked by default rather than silently permitted —
the safe direction for a security check to fail in.

### Writes hidden in CTEs

```python
def _reject_data_modifying_ctes(statement) -> None:
    for cte in statement.find_all(exp.CTE):
        if cte.this is not None and not isinstance(cte.this, ALLOWED_STATEMENTS):
            raise ValidationError(f"A WITH clause contains a {...} statement.")
```

```sql
WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone
```

Valid PostgreSQL. The outer statement is genuinely a `SELECT`, so the
top-level type check passes it. Every CTE body has to be checked too.

### Case-insensitive table matching

```python
permitted = {name.lower() for name in allowed_tables}
unexpected = {table for table in tables if table not in permitted}
```

SQL identifiers are case-insensitive unless quoted, so `FROM player` against a
table declared `Player` is a correct query that the database resolves happily.

This was a real bug. Table names parsed from SQL were lowercased, but the
allow-list kept its original case, so **every** query against a capitalised
schema was falsely rejected. On the BIRD benchmark it accounted for roughly a
quarter of all failures; fixing it moved accuracy from 42.5% to 50%. See
[07-decisions.md](07-decisions.md).

### CTE names are not tables

```python
cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
tables = {t.name.lower() for t in statement.find_all(exp.Table)} - cte_names
```

A CTE alias looks exactly like a table reference in the tree. Counting it would
make the allow-list reject valid queries that use CTEs.

### Row limits

```python
if isinstance(statement, (exp.Union, exp.Except, exp.Intersect)):
    wrapped = exp.select("*").from_(statement.subquery(alias="bounded")).limit(row_limit)
    return wrapped, True
```

A `LIMIT` attached to a `UNION` bounds only its final branch — a wrong answer
rather than an error, which is worse. Set operations are wrapped instead.

An existing *smaller* limit is respected: the model may have meant `LIMIT 10`,
and the cap is a ceiling, not a target.

---

## `db/dialects.py` — the differences, in one place

```python
@dataclass(frozen=True)
class PostgresDialect(Dialect):
    def session_setup(self, *, statement_timeout_ms: int) -> list[str]:
        return [
            f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}",
            "SET LOCAL transaction_read_only = on",
        ]
```

Almost everything else is dialect-agnostic because SQLAlchemy and sqlglot
absorb the differences. Two things do not:

**Session hardening.** SQLite has neither statement — it enforces read-only by
how the file is opened, which is what `read_only_url()` does.

**Error reading.** PostgreSQL returns a five-character SQLSTATE, which is
precise and stable. SQLite returns an English sentence, which is neither:

```python
message_patterns = (
    (r"no such table", KIND_MISSING_RELATION),
    (r"no such column", KIND_MISSING_COLUMN),
    ...
)
```

`classify_error` prefers SQLSTATE and falls back to matching the message.

---

## `db/execute.py` — running it safely

```python
with engine.begin() as connection:
    for statement in dialect.session_setup(statement_timeout_ms=statement_timeout_ms):
        connection.execute(text(statement))

    cursor = connection.execute(text(sql))
    columns = tuple(cursor.keys())

    fetched = cursor.fetchmany(row_limit + 1)     # ← one extra
    truncated = len(fetched) > row_limit
    rows = tuple(tuple(r) for r in fetched[:row_limit])
```

Fetching `row_limit + 1` is how truncation is known. Without the extra row, a
result of exactly 1000 rows is indistinguishable from a result of 50,000 capped
at 1000 — and the user would be shown a partial answer presented as complete.

### Compact errors

```python
def _classify(exc: DBAPIError, dialect: Dialect) -> ExecutionError:
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    primary = getattr(diagnostics, "message_primary", None)
    hint = getattr(diagnostics, "message_hint", None)

    message = primary or str(original or exc).strip().splitlines()[0]
    if hint:
        message = f"{message} (hint: {hint})"
```

Raw driver exceptions are verbose and include the full statement and a
traceback. That goes into a repair prompt, where it wastes context and buries
the one sentence that matters. PostgreSQL's `hint` is kept because it is
frequently good at suggesting the column you actually meant.

---

## `llm/mantle.py` — talking to the model

### Signing

```python
class SigV4Auth(httpx.Auth):
    requires_request_body = True

    def auth_flow(self, request: httpx.Request):
        aws_request = AWSRequest(
            method=request.method, url=str(request.url), data=request.content,
            headers={k: v for k, v in request.headers.items()
                     if k.lower() in {"content-type", "accept", "anthropic-version"}},
        )
        BotocoreSigV4Auth(self._credentials(), self._service, self._region).add_auth(aws_request)
        for key, value in aws_request.headers.items():
            request.headers[key] = value
        yield request
```

`requires_request_body = True` matters: SigV4 hashes the body, so it must exist
before signing. Only headers that should be signed are forwarded — httpx adds
hop-by-hop headers that must not be.

### Retry policy

```python
retryable = response.status_code == 429 or response.status_code >= 500
if not retryable:
    raise MantleError(f"Mantle returned HTTP {response.status_code}: {body}")
```

Throttling and server errors are retried with exponential backoff. Other 4xx
are not: a malformed request fails identically every time, and retrying only
delays the error.

Entitlement gets its own exception type:

```python
if response.status_code in (401, 403) and "not available for this account" in body:
    raise ModelUnavailableError(...)
```

Different remedy — choose another model or request access — so it should not
look like a transient failure.

### Tolerant reply parsing

```python
def extract_sql(text: str) -> str:
    if "```" in cleaned:
        ...  # take the first fenced block, drop a ```sql language tag
    return cleaned.rstrip(";").strip()
```

Models are asked for bare SQL and most comply. `mistral.ministral-3-8b-instruct`
wraps it in fences regardless. Being tolerant costs one small function; being
strict costs a spurious failure on an otherwise correct query.

---

## `agent_graph.py` — the orchestration

A LangGraph `StateGraph`. Nodes are built by factories closing over the agent,
because a node must have the signature `(state) -> dict` but needs the model
client, the database and the configuration.

```python
def make_generate_sql(agent: SqlAgent, emit):
    def generate_sql(state: AgentState) -> dict[str, Any]:
        ...
        return {"sql": extract_sql(completion.text)}     # only what changed
    return generate_sql
```

Each node returns a **partial** update, which LangGraph merges. That keeps
nodes honest about their own effects: `generate_sql` cannot quietly modify the
neighbourhood.

### The decision that justifies the graph

```python
def route_after_execute(state: AgentState) -> str:
    if state.get("last_error") is None:
        return "write_answer"
    if state.get("attempts", 0) > agent.config.max_repair_attempts:
        return "give_up"
    if state.get("failure_kind") in CONTEXT_FAILURES:
        return "widen_schema"       # Loop B — context was too narrow
    return "build_context"          # Loop A — SQL was wrong
```

One function, one place, four outcomes. The previous implementation made this
same decision inline inside two separate exception handlers, and one of them
omitted the context-failure case — so the most common trigger for widening
never widened. A conditional edge makes that class of bug structurally harder.

### Validation and execution share a node

```python
try:
    validated = validate(candidate_sql, ...)
    result = execute(agent.engine, validated.sql, ...)
except ValidationError as exc:
    return {"last_error": str(exc), "failed_sql": candidate_sql,
            "failure_kind": exc.kind, "attempts": attempts + 1}
except ExecutionError as exc:
    return {"last_error": str(exc), "failed_sql": validated.sql,
            "failure_kind": exc.kind, "attempts": attempts + 1}
```

They are one node because they share a failure shape: both produce "this
attempt failed, here is why, here is what kind of problem it was", and the
routing that follows treats them identically. Splitting them would mean
duplicating the routing.

### A gotcha

LangGraph evaluates the state schema's annotations for real when the graph is
built. A type imported only under `if TYPE_CHECKING:` fails there with
`NameError`. `Trace` and `Attempt` are therefore imported at runtime — safe
from circularity because `pipeline` imports `agent_graph` lazily, inside
`ask()`.

---

## `pipeline.py` — the public surface

Holds `SqlAgent.ask()`, the `Trace` and `AgentResult` types, and the individual
steps the nodes call (`select_seed_tables`, `build_context`, `pick_model`,
`write_answer`).

```python
def ask(self, question: str, *, on_progress=None) -> AgentResult:
    graph = build_agent_graph(self, emit)
    final = graph.invoke(
        {"question": question, "trace": trace, "attempts": 0},
        {"recursion_limit": 50},
    )
```

The graph is compiled per request because the progress hook differs per call.
Compilation is microseconds against several seconds of model calls, so caching
it would optimise the wrong thing.

`recursion_limit` is a backstop against a routing bug producing an endless
walk; the repair budget already bounds normal looping.

### Never raising

```python
try:
    final = graph.invoke(...)
except Exception as exc:
    return AgentResult(..., error=str(exc))
```

A question that cannot be answered is a normal outcome, not an exception. The
callers are a web handler and a benchmark runner, and both want a structured
result either way.

### Progress hooks cannot break a request

```python
def _safe_hook(hook):
    def safe(stage, detail):
        try:
            hook(stage, detail)
        except Exception:
            logger.debug(...)
    return safe
```

A closed websocket or a full queue must not turn a successful answer into a
failure.

---

## `api/app.py` — bridging sync into async

```python
queue: asyncio.Queue = asyncio.Queue()
loop = asyncio.get_running_loop()

def on_progress(stage: str, detail: dict) -> None:
    loop.call_soon_threadsafe(queue.put_nowait, (stage, detail))

async def run():
    result = await run_in_threadpool(agent.ask, question, on_progress=on_progress)
    await queue.put(("result", to_response(result).model_dump()))
    await queue.put(None)

async def events():
    while True:
        item = await queue.get()
        if item is None:
            break
        stage, detail = item
        yield f"event: {stage}\ndata: {json.dumps(detail, default=str)}\n\n"
```

`on_progress` is called from a **worker thread**, and `asyncio.Queue` is not
thread-safe. `loop.call_soon_threadsafe` hands the item to the event loop
thread correctly. Touching the queue directly would work most of the time and
corrupt state occasionally, which is the worst kind of bug.

---

## `summarise.py` — what the turn window drops

History is a sliding window of the last few turns. `evicted_turns()` returns
the complement — everything the window no longer shows — and `summarise()`
folds those into a standing note of what is still in force.

### The split is computed in one place

`evicted_turns()` and `render_conversation()` take the same `window` and must
agree about where the boundary is. A turn in both is duplicated context; a turn
in neither is silently forgotten, which is the bug the module exists to
prevent. Four tests assert the two partition the history exactly.

### `SummaryCache.nearest()` is what makes it incremental

The agent holds no per-conversation state between requests — history is rebuilt
from the database each time. So there is nothing to be incremental *against*
unless the cache can find it.

```python
def nearest(self, evicted) -> tuple[str, list[Turn]]:
    turns = list(evicted)
    for cut in range(len(turns) - 1, 0, -1):
        found = self._entries.get(self.key(turns[:cut]))
        if found is not None:
            return found, turns[cut:]
    return "", turns
```

Longest-prefix-first, so the usual case — one more turn evicted since the last
question — hits on the first lookup. The key is a digest of the *rendered*
turns, not of the objects: the objects are new on every request and only their
text is stable.

### The cap is on the reply

```python
if len(text) > MAX_SUMMARY_CHARS:
    clipped = text[:MAX_SUMMARY_CHARS]
    stop = clipped.rfind(". ")
    text = clipped[: stop + 1] if stop > MAX_SUMMARY_CHARS // 2 else clipped.rstrip()
```

Told-about caps are not caps. The sentence-boundary cut is not cosmetic: a note
ending mid-clause reads as a fact that was cut short, which is a different
claim from the one the model made.

### `NONE` clears rather than fails

A run of unrelated lookups carries nothing forward. The model replying `NONE`
empties the note instead of leaving it asserting something no longer true.

---

## `judge.py` — grading the sentence

Execution accuracy measures the SQL. The faithfulness guard checks figures
against rows. Neither can see an answer that quotes every number correctly and
answers a different question.

### Three booleans, not a score

`answers_question`, `complete`, `supported`. A 1–5 rating is uncalibrated
between runs and unactionable within one — "3.4" does not say what to fix.

### A missing field is ungraded, not passed

```python
for field in ("answers_question", "complete", "supported"):
    if not isinstance(payload.get(field), bool):
        return None
```

`isinstance(..., bool)` rather than truthiness, because `"yes"` is truthy and
would have scored as a pass. A judge that reports "fine" when it could not be
reached inflates exactly the number it exists to produce, so `JudgeReport`
counts `ungraded` beside `quality` rather than folding it in.

### `Verdict.from_defects` round-trips

A run is stored as outcomes, not verdicts, so re-deriving the report from a
results file goes back through the defect names. An unknown name raises rather
than being ignored — ignoring it would score a graded failure as clean.

---

## `drift.py` — has it started behaving differently

### Everything is a proportion

Five signals, one test. Latency and cost are continuous, and comparing their
means properly needs a rank test or a distributional assumption response times
do not satisfy. Each becomes "how often is it worse than this deployment's own
p90", which is a proportion — and the threshold comes from the data rather than
from someone's guess.

### The z-test, without a numerical stack

```python
pooled = (hits_a + hits_b) / (n_a + n_b)
variance = pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b)
if variance <= 0.0:
    return 1.0
z = (p_b - p_a) / math.sqrt(variance)
return math.erfc(abs(z) / math.sqrt(2.0))
```

`erfc` gives the normal tail exactly, so there is no table and no dependency.
Two rates that are both 0% or both 100% make the pooled variance zero and the
z-score undefined; returning 1.0 keeps a degenerate metric from taking the
whole report down.

### Both gates, always

`p < alpha and abs(change) >= min_change`. Significance alone fires on 4.0% →
4.4% at a few thousand questions. Effect size alone fires constantly at small
n. α is 0.01 rather than 0.05 because five metrics are tested together.

### A clarification is not a failure

Found by running this against the project's own history: the failure rate
appeared to rise from 1.6% to 35%, and 31 of those 35 were the agent asking a
question back. `failure_rate` now excludes them and `clarify_rate` counts them
separately. `CLARIFICATION_ERROR` lives in `clarify.py` and is imported by the
four places that compare against it, because it used to be a bare string in
each.

---

## `benchmarks/bird.py` — measuring it

### Comparing result sets, not SQL text

```python
def result_signature(rows, *, ordered: bool):
    normalised = [tuple(normalise(cell) for cell in row) for row in rows]
    return normalised if ordered else Counter(normalised)
```

Many different queries produce the same correct answer, so text comparison
would fail nearly all of them. Row order is ignored unless the gold query has
an `ORDER BY`, where order is part of the answer.

```python
def normalise(value):
    if isinstance(value, Decimal): value = float(value)
    if isinstance(value, float):   return round(value, 6)
    if isinstance(value, int):     return float(value)
```

`1`, `1.0` and `Decimal('1.00')` are the same answer. Rounding also absorbs
floating-point noise from differently-ordered arithmetic.

### Fixed sampling seed

```python
random.Random(args.seed).shuffle(questions)
questions = questions[: args.limit]
```

The same subset every run, so a score change reflects a code change rather than
a different sample.

---

Next: [5. The web interface](05-frontend.md)
