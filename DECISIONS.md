# Decisions

Judgment calls made while building, with the reasoning, so any of them can be reversed without
re-reading the code.

## Architecture

**LangGraph over a chain.** Validation, cost estimation and execution all route back to generation.
That is a cycle over mutable state, which LCEL chains cannot express; writing the loop in plain
Python would forfeit checkpointing, tracing and resume.

**State holds only plain values.** The checkpointer serialises state at every step, so the engine,
model and linker live on an `AnalystContext` instead.

**SQLite checkpointer, not Postgres.** Aperture must never require write access to the database it
analyses — that would contradict the read-only guarantee. It carries its own state at
`~/.aperture/state.db`. A Postgres checkpointer is a config change for multi-user deployments.

**No write credential at all.** The plan originally escalated write statements to a human approval
step backed by an owner connection. That was dropped: an owner credential sitting in `.env` next to
an LLM loop is a liability out of proportion to the feature. Writes are refused.

## Retrieval

**Lexical linking, not embeddings — for now.** The `VectorIndex` seam exists and embeddings drop in
behind it, but the lexical scorer plus a foreign-key walk answers every demo question correctly on a
56-table schema, and it has no model download, no torch import and no startup cost. Embeddings are
worth adding when a schema is large enough that name matching stops discriminating.

**NumPy index by default, Qdrant opt-in.** At roughly 650 vectors a brute-force scan is ~0.3ms. A
vector server earns its keep past tens of thousands of vectors; below that it is a dependency that
breaks `pip install and go`.

**Two-tier retrieval for large schemas.** Table-level documents first, column-level only inside
candidate tables. A 50,000-table warehouse embeds 50,000 table docs, never a million columns.

**Empty tables are excluded from graph expansion** unless seeded directly. They cannot contribute
rows, so they are pure prompt cost — but a question *about* an empty table still needs it, so that
the answer can be "that table has no rows".

**Every token of a value must appear before a literal is pinned.** Matching any token made "coupon
usage by customer" pin `CONFUSED_CUSTOMER`.

## Guards

**AST validation, not regex.** A comment before `DROP`, a `DELETE` inside a CTE, a second statement
after `;` — a regex misses all three.

**The cost guard distinguishes "invalid" from "too expensive".** PostgreSQL's `EXPLAIN` raises on an
undefined column, so a plain typo surfaces at the cost guard rather than at execution. The two need
different repair prompts and different retry budgets.

**Identifier repair is deterministic.** The schema already knows that the column is `"createdAt"`.
Only unambiguous case-insensitive matches are rewritten; two candidate spellings means reporting the
database's error instead of guessing.

**Values for sensitive and identifier columns are withheld from profiles.** A column of ten UUIDs is
enumerable and worthless in a prompt; a column of ten phone numbers is enumerable and harmful.

## Charts

**Deterministic chart choice, not model-generated.** Mark and encodings follow from column types and
cardinality, which is free, reproducible, and cannot reference a column that is not in the result.
`validate_spec` exists so a caller-supplied spec can still be checked field by field.

**A scalar renders as a stat tile,** because a bar chart with one bar communicates nothing.

## Evaluation

**BIRD Mini-Dev's SQLite split, not the PostgreSQL one.** It needs no data loading, and running the
benchmark through SQLite exercises the same code path as the PostgreSQL demo — which is what makes
the multi-dialect claim real rather than aspirational.

**Cost is extrapolated after twenty questions** and the run stops if the projection exceeds the
configured ceiling. The ledger replays its log at startup so the ceiling is cumulative across
restarts.

## Things deliberately not done

- **No anonymised demo database.** The owner decided to demo against the real data. Sensitive
  columns are withheld from prompts, but returned rows are not redacted, and traces leave the
  machine when LangSmith is enabled.
- **No LangSmith key in the environment yet.** Everything runs without it; tracing switches on when
  `LANGSMITH_API_KEY` is set in `backend/.env`.
- **No Snowflake or BigQuery.** Driver installs and cloud credentials, for no demo value.
