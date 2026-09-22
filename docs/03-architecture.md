# 3. Architecture

From the top down: the shape of the system, then the lifecycle of one request,
then each subsystem in detail.

---

## The shape

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Interfaces                                  │
│   CLI (cli.py)        HTTP + SSE (api/app.py)        Benchmark       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  all three call the same object
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│              SqlAgent (pipeline.py) → LangGraph (agent_graph.py)     │
│  Compiled state graph: 10 nodes, 3 repair loops as conditional edges │
└──┬──────────────┬──────────────┬───────────────┬────────────────┬────┘
   │              │              │               │                │
   ▼              ▼              ▼               ▼                ▼
┌────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────┐  ┌──────────┐
│ schema │  │ prompts  │  │    llm    │  │    guards    │  │    db    │
│        │  │ glossary │  │           │  │              │  │          │
│introspec│ │ Every    │  │ Mantle    │  │ validator    │  │ execute  │
│ graph  │  │ prompt,  │  │ client,   │  │ cost         │  │ sample   │
│retrieval│ │ versioned│  │ SigV4     │  │ arithmetic   │  │ dialects │
│        │  │          │  │           │  │ critic       │  │          │
│        │  │          │  │           │  │ faithfulness │  │          │
│        │  │          │  │           │  │ prescreen    │  │          │
└────────┘  └──────────┘  └───────────┘  └──────────────┘  └──────────┘
     │                          │                              │
     ▼                          ▼                              ▼
  Database                   Bedrock                       Database
  catalogue                   Mantle                        (queries)
```

Two more interfaces sit alongside the three above: an **MCP server**
(`mcp_server.py`), which offers the agent as tools to another assistant without
handing it database credentials, and the **conversation** layer
(`conversation.py`), which threads previous turns into the prompt so a follow-up
like "and for April?" is answerable.

Three properties of this layout are deliberate:

**One agent, three interfaces.** The CLI, the web API and the benchmark all
construct `SqlAgent` and call `ask()`. There is no logic in an interface that
the others lack, so a benchmark score genuinely reflects what a user gets.

**Dependencies point inward.** `pipeline.py` imports from `schema`, `llm`,
`guards` and `db`; none of them import `pipeline`. Every leaf module is
testable alone, and none of them know an agent exists.

**Everything expensive is injected.** `SqlAgent(engine, client=..., config=...)`
takes its database connection, its model client and its settings as arguments.
That is what makes the pipeline tests possible: they pass a `FakeClient` with
scripted replies and a real database, so the repair loops can be tested
deterministically.

---

## The full graph

Optional nodes are marked. Each is off by default and each is a straight
pass-through when off, so the graph has one shape rather than one per
combination of flags — a toggle cannot change the routing by accident.

```
  screen ─────────refused / needs clarification────────────► give_up
    │   (optional: pre-screen, ambiguity check)
    ▼
  check_cache ────hit────────────────────────────┐
    │   miss                                     │
    ▼                                            │
  select_tables ──no table matched──► give_up    │
    │                                            │
    ▼                                            │
  build_context ◄───────── Loop A ───────────┐   │
    │                                        │   │
    ▼                                        │   │
  generate_sql   (optional: vote over N)     │   │
    │                                        │   │
    ▼                                        │   │
  criticise ─────rejected──── Loop C ────────┤   │
    │   (optional)                           │   │
    ▼                                        │   │
  validate_and_execute ◄─────────────────────┼───┘
    │  validator → cost gate → execute       │
    ├──sql / cost failure──────────────────► │
    ├──context failure──► widen_schema ────► │ Loop B
    ├──out of budget ───► give_up            │
    ▼
  write_answer   (optional: faithfulness check, one retry)
    │
    ▼
   END
```

A **multi-part question** takes a different path entirely, above the graph:
`ask()` splits it, runs each part through this same graph, and synthesises one
answer. A sub-question is just a question, so it gets the same retrieval, the
same validator and the same cost gate rather than a reduced pipeline with
weaker guarantees.

---

## The lifecycle of one question

Walking through `SqlAgent.ask("What is the total order value per region?")`.

### Before any request: warming up

```python
agent.warm()
```

Reflects the whole schema and builds the graph. Done once at startup, not per
request — it involves a batch of catalogue queries and would otherwise add
seconds to every question. The result is immutable and shared.

### Step 1 — which tables?

Two paths, chosen by size.

**Small schema** (at or below `full_schema_threshold`, default 15 tables):
skip the question entirely and use every table.

```python
if len(self.snapshot) <= self._config.full_schema_threshold:
    seeds = sorted(self.snapshot.tables)
    neighbourhood = expand(self.graph, seeds, max_hops=0)
```

This saves a model call and removes a whole class of failure. It was added
after measurement, not from first principles — see
[07-decisions.md](07-decisions.md).

**Large schema**: ask the model, sending only table *names* (a few hundred
tokens, not several thousand):

```
Given these database tables:
  addresses
  customers
  orders
  ...
Question: What is the total order value per region?
Reply with JSON only: {"tables": ["name", ...]}
```

Two safeguards around that call:

- Names the model returns that do not exist are discarded. A hallucinated
  table must not reach the graph walk.
- If the reply cannot be parsed at all, a deterministic fallback matches table
  names appearing in the question text, including singular forms ("customer"
  finds `customers`). Crude, but it keeps the agent working when the model
  misbehaves, and it makes the unit tests independent of model behaviour.

### Step 2 — walk the graph

```python
neighbourhood = expand(self.graph, seeds, max_hops=1)
```

Breadth-first from the seed tables, one hop by default. Given `customers`, one
hop reaches `orders` (which references it) and `regions` (which it
references) — note both directions.

The result records hop distance per table, which is used to order them in the
prompt: nearest first, because models weight earlier context more heavily.

No model call, no database call. Pure in-memory graph work, measured in
microseconds.

### Step 3 — sample rows

```sql
SELECT * FROM customers LIMIT 2
```

Run per selected table, in a read-only transaction with a short timeout.

The schema says `status` is `VARCHAR(20)`. It does not say whether the values
are `'active'` or `'A'` or `1`. Two rows answer that, and a `WHERE` clause
that assumes wrong matches nothing.

This query is **code-generated from a fixed template**, never written by the
model. A model that could choose its own sample query could be talked into
sampling something it should not.

Failure here is deliberately non-fatal: if a table cannot be sampled, the
pipeline continues without it. A sample is an optimisation, not a requirement.

### Step 4 — generate SQL

The prompt contains the dialect, the chosen tables with columns and types, the
**exact join predicates** taken from real foreign-key constraints, the sample
rows, and the question.

The join predicates are the single highest-value part:

```
Relationships (use these exact join conditions):
  orders.customer_id = customers.id
  customers.region_id = regions.id
```

Without them the model infers joins from column names, which is where naive
text-to-SQL most often goes wrong — and silently, because a wrong join usually
returns rows rather than an error.

Model tier is chosen by table count: a one- or two-table question uses the
light model, more than that uses the strong one. A crude proxy for difficulty,
but free — deciding properly would cost another model call.

### Step 5 — validate

[`guards/validator.py`](../src/sqlagent/guards/validator.py), no model
involved:

1. Parse. Unparseable is refused.
2. Exactly one statement.
3. That statement is a `SELECT`/`UNION`/`EXCEPT`/`INTERSECT`. Anything else is
   refused, and the refusal *names the type* so a repair attempt has something
   to work with.
4. No common table expression contains a write.
5. No forbidden functions (`pg_read_file`, `dblink`, `pg_sleep`, …).
6. Every table read is one the model was shown — **compared
   case-insensitively**, because `FROM player` against a table declared
   `Player` is a correct query.
7. A `LIMIT` is present and small enough; one is added if missing. `UNION`
   queries are wrapped rather than modified in place, since attaching a `LIMIT`
   to a union bounds only its final branch.

### Step 6 — execute

```python
with engine.begin() as connection:
    connection.execute(text("SET LOCAL statement_timeout = 30000"))
    connection.execute(text("SET LOCAL transaction_read_only = on"))
    cursor = connection.execute(text(sql))
    fetched = cursor.fetchmany(row_limit + 1)
```

The session-hardening statements are dialect-specific and come from
[`db/dialects.py`](../src/sqlagent/db/dialects.py) — SQLite has neither, and
enforces read-only by how the file is opened instead.

Fetching `row_limit + 1` rows is how truncation is detected honestly: if the
extra row exists, the result really was larger, and the user is told so rather
than being shown a capped result presented as complete.

On failure, the exception is reduced to one actionable line — SQLSTATE where
available, PostgreSQL's own hint when it offers one (it is often good at
suggesting the column you meant), and never a traceback.

### Step 7 — answer

If the result is empty, no model is called at all: there is nothing to
summarise, and a model handed zero rows tends to invent an explanation for
them.

Otherwise the question, the SQL and a preview of the rows go to the light
model, with a system prompt that says: answer directly, quote the real numbers,
never invent a figure.

---

## The two repair loops

The part most worth understanding, and the reason the orchestration is a graph:
each loop is a conditional edge out of `validate_and_execute`, decided in one
function (`route_after_execute`) rather than at each failure site.

```
                    ┌─────────────────┐
                    │  generate SQL   │◄──────────────┐
                    └────────┬────────┘               │
                             ▼                        │
                    ┌─────────────────┐               │
                    │    validate     │               │
                    └────────┬────────┘               │
                             ▼                        │
                    ┌─────────────────┐               │
                    │     execute     │               │
                    └────────┬────────┘               │
                             │                        │
              ┌──────────────┴──────────────┐         │
              ▼                             ▼         │
          success                        failure      │
              │                             │         │
              ▼                             ▼         │
        write answer               classify the error │
                                             │        │
                    ┌────────────────────────┴────┐   │
                    ▼                             ▼   │
        table_not_allowed                  syntax     │
        missing_relation                   bad_function
        missing_column                     timeout    │
                    │                      wrong type │
                    ▼                             │   │
            ┌───────────────┐                     │   │
            │ widen by      │                     │   │
            │ one hop       │─────────────────────┴───┘
            │  (Loop B)     │          (Loop A)
            └───────────────┘
```

### Loop A — the SQL was wrong

The context was sufficient; the statement was not. Retry with the same schema
plus the error message:

```
This query failed:
SELECT nonexistent_col FROM customers

Error: column "nonexistent_col" does not exist (hint: perhaps you meant "name")

Write a corrected SQL SELECT statement.
```

### Loop B — the context was insufficient

The query named something real that was never shown. Retrying with identical
context would fail identically, so the schema walk widens by one hop first, and
*then* regenerates.

The trigger comes from three error kinds:

| Kind | Source | Meaning |
|---|---|---|
| `table_not_allowed` | The validator | Named a table outside the provided set — caught before any database round trip |
| `missing_relation` | The database | `42P01` / `no such table` |
| `missing_column` | The database | `42703` / `no such column` |

That first one deserves emphasis. It was originally handled as a Loop A case,
because it comes from the validator rather than the database — which meant
widening never fired for the cheapest and most common signal. A test caught it.
The classification now lives in one place, `CONTEXT_FAILURES`, reached by both
the static and runtime paths.

### One budget, shared

Both loops draw on `max_repair_attempts` (default 3). A question that needs
four attempts is a question to hand back to the user, whichever kind of repair
each attempt was. When the budget runs out, the response says what was tried
and quotes the last error — often enough for the user to rephrase successfully.

---

## The trace

Every request builds one `Trace`:

```python
@dataclass
class Trace:
    question: str
    seed_tables: list[str]
    candidate_tables: list[str]
    hops: int
    sampled: bool
    attempts: list[Attempt]      # sql, ok, error, error_kind, which loop
    input_tokens: int
    output_tokens: int
    seconds: float
    model_calls: int
```

One object, not scattered log lines. Three things fall out of it for free:

- **Debugging** — a wrong answer is diagnosed by reading one record: which
  tables were offered, what SQL was produced, what failed, what was retried.
- **Metrics** — repair rate, token cost, latency are all derivable. No separate
  counters to keep in sync.
- **Evaluation** — the benchmark reads the same structure, so a benchmark run
  and a production request are measured identically.

---

## Where each concern lives

| Concern | Module | Notes |
|---|---|---|
| What tables exist | `schema/introspect.py` | Reflection; immutable snapshot; version hash |
| What joins to what | `schema/graph.py` | NetworkX; handles cycles and self-references |
| Which tables matter | `schema/retrieval.py` | Bounded bidirectional BFS |
| What the model is told | `prompts.py` | Every prompt, version-controlled |
| Talking to the model | `llm/mantle.py` | SigV4, retries, reply parsing |
| Is this statement safe | `guards/validator.py` | sqlglot; the security boundary |
| Running it safely | `db/execute.py` | Timeouts, row caps, read-only transaction |
| Per-database differences | `db/dialects.py` | Session hardening, error classification |
| Showing real values | `db/sample.py` | Fixed template; never model-authored |
| The sequence | `agent_graph.py` | LangGraph state graph: nodes and conditional edges |
| Public surface, trace, steps | `pipeline.py` | `SqlAgent.ask()`, `Trace`, the functions nodes call |
| Configuration | `config.py` | Validated at startup |

---

## Schema versioning

The snapshot carries a fingerprint — a hash over table names, column names and
types, nullability, primary keys and foreign keys. Deliberately *not* over row
counts or statistics, which change constantly and would invalidate caches for
no reason.

It exists so that caching can be correct. A cached schema graph, a cached
generated query or a cached sample is only valid for the schema it was built
against; when the hash changes, everything derived from it is stale together.

The hash is computed from reflected structure rather than from DDL event hooks,
because not every deployment exposes those, and a missed event fails silently
by serving a stale schema. Re-hashing periodically is cheap and
self-correcting.

---

Next: [4. Code walkthrough](04-code-walkthrough.md)
