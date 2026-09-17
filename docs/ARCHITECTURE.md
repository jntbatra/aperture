# Aperture — architecture and design

How the system works, why each part exists, and what it cannot do. Written to be
read by someone deciding whether the design is sound, not to sell it.

---

## 1. The problem

Asking a language model for SQL is easy. Trusting the answer is not, and the
failure modes are unequal:

| failure | frequency in our benchmark | catchable by |
|---|---|---|
| Query does not run | 10% of failures | validation, error feedback |
| Query runs and answers the wrong question | **90% of failures** | grounding, verification, asking |

That ratio decided the architecture. Syntax errors are loud and self-announcing;
a plausible query answering a slightly different question is silent, and no
amount of retrying fixes it. So the effort went into three places: making the
model's input reflect what is actually in the database, checking results
arithmetically after execution, and asking a question when the request is
genuinely ambiguous.

## 2. Design principles

**Prefer determinism to prompting.** Anything computable from the schema or the
data is computed. Identifier casing, chart choice, empty-result diagnosis, join
fan-out and ambiguity detection are all code, not instructions. Prompts are used
where judgment is genuinely required.

**Enforce safety in the database, not the prompt.** A prompt that says "do not
write" is a request. A role without write permission is a guarantee.

**Ground every claim in the data.** Observed enum values, real date ranges, real
row counts. A model that has seen `CONFUSED_CUSTOMER` does not guess
`'completed'`.

**Show the SQL, always.** Every answer carries the query that produced it and a
one-line statement of the assumptions made. A wrong answer a user can see is
recoverable; a wrong answer they cannot is not.

**Fail loudly and specifically.** A failed connection says so and stops, rather
than consuming a repair budget and reporting "I could not produce a working
query".

## 3. The graph

```
question
   │
   ▼
 route ─── chitchat / "what can you do" ──────────────────► small_talk ──► END
   │ query
   ▼
link_schema ──► clarify ── ambiguous ──────────────────────► END (asks the user)
                   │ clear
                   ▼
              generate_sql ◄──────────────────────┐
                   │                              │
                   ▼                              │
               validate ── write/banned ──► END   │
                   │ ok                           │
                   ▼                              │
              cost_guard ── unreachable ──► END   │ repair, ≤3 attempts
                   │ ok                           │
                   ▼                              │
                execute ──── error ───────────► diagnose
                   │ rows          ▲              ▲
                   │               │ empty result │
                   ├─ 0 rows ──► diagnose_empty ──┘
                   ▼
                verify ──► narrate ──► chart ──► END
```

The cycle is the reason for LangGraph. Validation, cost estimation and execution
each route back into generation carrying the database's own error. A chain
cannot express that without moving the loop into Python and losing checkpointing,
tracing and resume with it.

### Node responsibilities

| node | does | cost |
|---|---|---|
| `route` | classifies chitchat vs question; resets per-turn state | free |
| `link_schema` | selects tables, pins literals, attaches metric definitions | free |
| `clarify` | asks one question if the schema shows ambiguity | free |
| `generate_sql` | writes SQL, or reuses a cached query | 1 model call |
| `validate` | AST checks, table-existence check, identifier repair | free |
| `cost_guard` | `EXPLAIN` before running | 1 round trip |
| `execute` | runs read-only with a timeout | 1 round trip |
| `diagnose` | decides whether and how to retry | free |
| `diagnose_empty` | explains zero rows from the profile | free |
| `verify` | arithmetic post-checks and result insights | 2 round trips |
| `narrate` | writes the prose answer | 1 model call |
| `chart` | picks a Vega-Lite spec, suggests follow-ups | free |

A typical answered question costs **two model calls**.

## 4. Grounding the model

### Schema introspection

The catalog is read in a fixed number of queries regardless of table count —
`pg_class`, `pg_attribute`, `pg_constraint`, `pg_enum` — so introspection is flat
in schema size. 56 tables and 78 foreign keys take ~0.04s. SQLite and MySQL fall
back to SQLAlchemy reflection, which is per-table but only used at smaller scale.

### Profiling

Value linking is the dominant failure mode in text-to-SQL, so the data is
profiled before anything is asked. On PostgreSQL this reads **`pg_stats`** — the
statistics the planner has already computed — giving most-common values, distinct
counts and null fractions with no table scans. Exact row counts and temporal
ranges are queried directly, then the whole snapshot is cached per database
fingerprint (0.13s cold, 0.003s warm).

Withheld from prompts: columns matching a PII pattern (email, phone, address…)
and identifier columns, whose values are high-entropy and useless as vocabulary.

### Retrieval: lexical seeding plus a foreign-key walk

1. Tables are scored by token overlap with the question, weighted toward table
   names, penalised if empty.
2. Observed values are matched against the question — "delivered" pins
   `orders.status = 'DELIVERED'`. Every token of a value must appear, so "coupon
   usage by customer" does not match `CONFUSED_CUSTOMER`.
3. Matched metric definitions contribute their tables as seeds, because "revenue"
   names no table.
4. The seeds are expanded by BFS over the foreign-key graph, skipping empty
   tables, up to a budget.

The walk is the part similarity search cannot replace: asking for revenue per
kitchen retrieves `orders` and `kitchen_profiles` by name, and misses
`item_kitchens`, the junction table the query must pass through.

**Every table name in the database also ships with the prompt.** Detailed DDL
goes only to the linked subset, but the full inventory costs ~300 tokens and
removes the model's need to guess whether a table exists. Before this, the model
invented `FROM products` on a schema whose table is `items`.

### Semantic layer

`semantic.yaml` holds definitions the schema cannot supply: that revenue counts
delivered orders only, that amounts are stored in paise, that a disputed order is
neither a sale nor a cancellation. Only definitions matching the question enter
the prompt.

Definitions are scoped **by column, not by table name**. An uploaded workbook
with a sheet called `orders` once inherited a rule that amounts were in paise and
reported a spreadsheet of dollars divided by 100. A definition now applies only
when every column it references exists.

## 5. Safety

Three independent layers, each sufficient on its own to stop a write:

1. **A database role that cannot write.** `GRANT SELECT` only, plus
   `default_transaction_read_only`. `DELETE` fails at the server with
   `cannot execute DELETE in a read-only transaction`. This does not depend on
   any prompt or parser being correct.
2. **AST validation with `sqlglot`.** Rejects writes anywhere in the tree
   (including a `DELETE` hidden inside a CTE), stacked statements, `SELECT …
   INTO`, locking reads, filesystem and sleep functions, and bare `SELECT` with
   no projection. Injects a row limit. Reports a typed failure kind so the loop
   can branch. A regex misses all three of the first cases; a parser cannot.
3. **A planner cost gate.** `EXPLAIN (FORMAT JSON)` before execution; plans above
   a cost threshold are refused without touching the data.

Plus a statement timeout, a row limit, and a spend ledger that replays its log on
startup so a hard ceiling survives a crash mid-run.

There is deliberately **no write credential anywhere in the system**. Write
statements are refused, not escalated.

## 6. Self-correction

The loop carries structured errors, not strings. `psycopg`'s diagnostics are
parsed into `sqlstate`, `message_primary`, `hint`, `detail` and `position`, and
each class gets a specific corrective:

| sqlstate | meaning | response |
|---|---|---|
| `42703` | undefined column | exact spelling, quote camelCase |
| `42P01` | undefined table | list the tables that exist |
| `22P02` | invalid literal for type | show observed values |
| `42803` | grouping error | every selected expression must be grouped or aggregated |
| `57014` | timeout | narrow, do not rewrite |
| `28xxx`, `08xxx` | auth or connection failure | **stop** — not a query problem |

**Convergence guards**, each for a real observed failure:

- *Identical SQL returned* — does not consume an attempt, but forces a
  temperature change; at temperature 0 the same prompt reproduces itself exactly.
- *Same error twice* — stop rewriting and widen the schema instead.
- *Prose instead of SQL* — its own failure class and corrective.
- *Truncated output* — retried with a larger budget, never treated as repairable.
- *Final attempt* — asks for the simplest formulation, since stacked CTEs were a
  recurring failure shape.

**Deterministic identifier repair.** PostgreSQL folds unquoted identifiers to
lower case, so `createdAt` becomes `createdat` and is not found. Rather than
spend an attempt rediscovering the spelling, the AST is corrected from the schema
— unambiguous matches only. This removed the failure class outright: questions
that previously exhausted two attempts now answer first time.

## 7. After execution

### Verification

Two probes derived from the query's own AST, run as counts:

- **Join fan-out** — `COUNT(*)` against `COUNT(DISTINCT base primary key)`. A
  child table larger than its parent multiplies rows, so a `SUM` over parent
  columns is inflated by that ratio. Measured on the demo schema: 2,999 orders
  become 16,812 joined rows, a **5.6×** inflation, with no error raised.
- **Rows discarded by a join** — row counts with and without the joins. An inner
  join keeps only matching rows, so joining orders to riders answers about
  **646 of 2,999 rows (78% discarded)** while looking entirely normal. Skipped
  for `LEFT` joins, which keep unmatched rows by definition.

A model asked for a second opinion can agree with the first mistake. Counting
cannot.

*Note on a corrected check:* an earlier version claimed a `GROUP BY` dropped
NULL-keyed rows. It does not — PostgreSQL keeps them as their own group, and the
grouped counts sum to every row. The check now reports what is true: a large
unlabelled NULL bucket is counted but is not a category.

### Zero rows

The most likely failure on real data, and not an error. Diagnosed from the
profile before any model call:

- the table is empty — *"refunds contains no data at all; this is a property of
  the database, not of the query"*
- the literal never occurs — *"orders.status never holds 'COMPLETED'; observed
  values are DELIVERED, CANCELLED, …"*
- the date filter is outside the data — *"orders.createdAt covers 2026-05-09 to
  2026-09-17"*

A scalar zero is treated the same way: "0 orders in January 2019" is correct and
useless without the reason.

### Charts and insights

Charts are chosen from the result's column types and cardinality — temporal plus
measure gives a line, low-cardinality category gives a bar, a lone scalar gives a
stat tile — and emitted as a **validated Vega-Lite spec**. A model that emits
Python to draw a chart is a remote-code-execution hole.

Insights are arithmetic: concentration, monotonic trend, outliers. A model asked
to find something interesting always finds something.

## 8. Asking instead of guessing

When the schema shows the question is underspecified, one question is more honest
than an interpretation buried in an assumptions line. The rules are few and
high-precision, because a tool that asks about everything gets ignored:

- a relative date ("last month") with more than one populated event date on the
  primary table → *which date should I measure by?*
- a ranking with no measure named → *ranked by what?*

Attribute timestamps (`deletedAt`, `bannedAt`) are never offered, a question that
names its own date column is not ambiguous, and a matching metric definition
settles it without asking. The model may also reply `CLARIFY: …` instead of SQL.

**Benchmarks set `APERTURE_CLARIFY=false`** — there is nobody to ask, and each
question is complete by definition.

## 9. Product surfaces

One guarded core, four ways in:

| surface | entry point |
|---|---|
| CLI | `aperture ask "…"` with a live node timeline, `--csv` export |
| Web app | `aperture serve` — chats, charts, caveats, follow-up chips |
| HTTP API | `POST /ask` (SSE), `/schema`, `/connections`, `/upload`, `/usage` |
| MCP | `aperture mcp` — `ask_database`, `run_sql`, `describe_schema` |

**Data in:** direct connections to PostgreSQL, MySQL and SQLite; uploads of CSV,
Excel (one table per sheet), SQLite files, and plain-text `pg_dump` output.
`pg_dump -Fc` archives are rejected with an explanation rather than half-parsed.

**State:** conversations, messages and per-user connections in SQLite; LangGraph
checkpoints in a separate SQLite file; a schema-versioned query cache that stores
the chosen SQL but never the rows, so a repeat skips generation and still reads
live data (6.5s → 1.8s).

**Accounts:** Google sign-in is optional, verified server-side against Google's
public keys with no client secret. With none configured, everything works as a
local profile. Storage is keyed by user id throughout, so hosting later is
configuration rather than a rewrite.

## 10. Evaluation

**Harness:** BIRD Mini-Dev, 500 questions across 11 databases the system has
never seen, scored by **execution accuracy** — gold and predicted SQL are both
run and their result sets compared as multisets, so a differently-written correct
query counts. The benchmark runs through the same `Database`, validator and graph
as the PostgreSQL demo, via SQLite.

**Result: 266/500 = 53.2%.**

| database | accuracy |
|---|---|
| superhero | 80.8% |
| european_football_2 | 68.6% |
| student_club | 64.6% |
| toxicology | 57.5% |
| financial | 50.0% |
| codebase_community | 49.0% |
| debit_card_specializing | 46.7% |
| thrombosis_prediction | 46.0% |
| formula_1 | 42.4% |
| card_games | 42.3% |
| california_schools | 26.7% |

The spread is informative. Conventional relational schemas do well. The weak
ones are weak for identifiable reasons: `debit_card_specializing` stores dates as
`YYYYMM` strings, `thrombosis_prediction` uses opaque medical column codes, and
`california_schools` questions lean on external domain knowledge. All three are
cases a semantic layer would address and BIRD deliberately does not provide one.

**Failure composition:** of 181 failures analysed, **162 (90%) produced clean,
runnable SQL that answered the wrong question**; only 19 failed to produce
runnable SQL at all. Guardrails and repair had already solved the second problem.

**A negative result worth recording.** Execution-guided self-consistency —
sampling three candidates, running each, and choosing the result most agree on —
was measured against the same 100 questions: **60% → 62%, +2 points**, fixing 7
and breaking 5, at three times the generation cost. On a sample of 100 that
difference is not distinguishable from noise. The likely reason is that
candidates sampled from one model on one prompt are not independent: the same
misreading recurs and outvotes a correct minority. It remains available behind
`APERTURE_CANDIDATES` but is not the default. Cross-model voting is the version
worth testing.

**Cost:** roughly $0.003 per question at deliberately pessimistic configured
rates. Bedrock does not expose per-model pricing through the runtime API, so the
ledger treats its dollar figure as an estimate; token counts are exact.

## 11. Limits

- **Semantic correctness is unsolved.** Nine of ten failures are queries that run
  and mislead. Verification, the semantic layer and clarification narrow it; none
  eliminates it.
- **No cross-dataset joins.** A chat is pinned to one connection. Federating two
  engines needs a shared planner.
- **Retrieval is lexical.** An embedding index sits behind the same interface but
  is not the default: on a 56-table schema the foreign-key walk matters more than
  similarity, and the full table inventory removes most of the need.
- **Scale ceiling on the inventory.** Shipping every table name holds to roughly
  a few thousand tables. Past that it needs tiering by schema.
- **Traces leave the machine** when LangSmith is enabled, and result rows reach
  the model provider. Sensitive columns are withheld from prompts; returned rows
  are not redacted.
- **`pg_dump -Fc` is unsupported** — it needs `pg_restore` and a live server.

## 12. Repository map

```
backend/aperture/
  config.py          settings, all APERTURE_-prefixed
  db/                connection, structured errors, introspection, profiling, cache
  guards/            AST validation, planner cost gate
  schema/            foreign-key graph, lexical linker
  semantic/          metric definitions and column-scoped loading
  graph/             state, prompts, nodes, wiring, empty-result diagnosis
  charts/            chart rules and Vega-Lite specs
  verify.py          post-execution arithmetic checks
  clarify.py         ambiguity detection
  vote.py            execution-guided self-consistency
  insights.py        deterministic result patterns
  ingest.py dumps.py CSV, Excel, SQLite, pg_dump loading
  cache.py           schema-versioned query cache
  store.py           users, conversations, messages, connections
  auth.py            optional Google sign-in
  server.py cli.py mcp_server.py
benchmarks/bird.py   BIRD Mini-Dev harness
frontend/            Vite + React + Tailwind + Vega
```

124 tests, none requiring a live database.
