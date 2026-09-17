# Aperture

Ask a SQL database questions in plain English. Aperture retrieves the relevant subschema, writes
SQL, validates it against a parsed syntax tree, runs it read-only, reads the database's own error
and repairs itself, then answers with a chart.

Built on **LangGraph**, **LangChain**, **LangSmith** and **AWS Bedrock**. Works against
PostgreSQL, SQLite and MySQL.

```
$ aperture ask "what was our revenue last month?"
  › routing
  › linking schema (12 tables · revenue)
  › writing SQL
  › validating (repaired 1 identifier)
  › estimating cost (cost 236)
  › executing (1 rows in 14ms)
  › summarising
  › choosing chart

  SELECT SUM("totalAmount") / 100.0 AS revenue
  FROM orders
  WHERE status = 'DELIVERED'
    AND "createdAt" >= DATE_TRUNC('MONTH', CURRENT_DATE) - INTERVAL '1 MONTH'
    AND "createdAt" <  DATE_TRUNC('MONTH', CURRENT_DATE)
  LIMIT 1000

  Revenue last month was Rs 172,617.44.
  assumptions: delivered orders only, amounts converted from paise
```

## Why this is not another text-to-SQL demo

**It cannot write, and that is enforced three times over.** The connection uses a database role with
`SELECT` only and `default_transaction_read_only`, so `DELETE` fails at the server. Before that, a
`sqlglot` AST check rejects writes, stacked statements, `SELECT ... INTO`, locking reads and
filesystem functions — a regex guard misses a `DELETE` hidden inside a CTE; a parser cannot. Before
*that*, the planner is asked what the query will cost and expensive plans are refused.

**It knows what the data actually contains.** Schema alone would let a model write
`status = 'completed'` against a database whose enum only ever holds `DELIVERED` and
`CONFUSED_CUSTOMER`. Aperture profiles the database first — borrowing PostgreSQL's own `pg_stats`,
so it costs no table scans — and puts observed values, date ranges and row counts into the prompt.

**It retrieves across the foreign-key graph, not just by similarity.** Asking for revenue per
kitchen returns `orders` and `kitchen_profiles` by name matching, and misses `item_kitchens`, the
junction table the query must join *through*. Aperture seeds tables from the question, then walks
foreign keys to pull in the join path, skipping tables it knows are empty.

**It repairs deterministically where it can.** PostgreSQL folds bare identifiers to lower case, so
`createdAt` becomes `createdat` and the column is not found. Rather than spend a repair attempt
rediscovering that, Aperture rewrites the AST from the schema it already has. This removed the
failure class outright: questions that previously exhausted two repair attempts now answer first
time.

**It explains empty results instead of answering "0".** Zero rows is the most likely failure on a
real database and it is not an error. Aperture diagnoses the cause from the profile before calling a
model at all: the table is empty, the literal never occurs in that column, or the date filter falls
outside the data's range.

> *how many orders were placed in January 2019?*
> **0 orders were placed in January 2019.** No rows because the date filter falls outside the data:
> `orders."createdAt"` covers 2026-05-09 to 2026-09-17.

**It warns about joins that silently lie.** A child table larger than its parent multiplies rows, so
any `SUM` over the parent is inflated — no error, just a wrong number. Aperture detects the ratio
from real row counts and says so in the prompt and in the answer.

**Charts are data, not code.** The output is a validated Vega-Lite spec, chosen deterministically
from the result's column types and cardinality. An agent that emits Python to draw a chart is a
remote-code-execution hole.

**Spend is bounded by a ledger, not by attention.** Every model call records its tokens to a log
that is replayed at startup, so a hard ceiling survives a crash and restart mid-run.

## Why LangGraph

Retry-until-valid is a cycle over mutable state. Validation, cost estimation and execution each
route back to generation carrying the database's own error, with `sqlstate` and `HINT` intact:

```
question → route → link_schema → generate_sql → validate → cost_guard → execute → narrate → chart
                        ▲                          │            │          │
                        └────── diagnose ◄─────────┴────────────┴──────────┘
                                                                └→ diagnose_empty
```

A chain would push that loop into plain Python outside the framework and lose checkpointing,
tracing and resume with it. The convergence guards are the interesting part:

| Failure | What happens |
|---|---|
| Identical SQL returned | Does not consume an attempt; temperature is raised so the retry can differ |
| Same error twice | Stops rewriting SQL and widens the schema instead |
| Prose instead of SQL | Separate failure class with its own corrective |
| Output truncated | Retries with a larger token budget — never treated as repairable |
| Zero rows | Diagnosed from the profile; retried at most once, on its own counter |

## Quick start

```bash
# 1. a read-only role for whatever database you are pointing at
psql -c "CREATE ROLE aperture_ro LOGIN PASSWORD '...'"
psql -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO aperture_ro"
psql -c "ALTER ROLE aperture_ro SET default_transaction_read_only = on"

# 2. install
cd backend && uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env    # set APERTURE_DATABASE_URL, AWS creds come from the usual chain

# 3. ask
aperture profile                       # what Aperture knows about your database
aperture link "revenue by kitchen"     # which tables that question retrieves, and why
aperture ask "how many orders were delivered last month?"
```

Web UI:

```bash
aperture serve                 # FastAPI on :8000
cd frontend && npm install && npm run dev   # Vite on :5173
```

As an MCP server, so any agent or IDE can query the database behind the same guardrails:

```json
{ "mcpServers": { "aperture": { "command": "aperture", "args": ["mcp"] } } }
```

## Configuration

Everything is `APERTURE_`-prefixed environment or `.env`. The ones that matter:

| Setting | Default | Notes |
|---|---|---|
| `APERTURE_DATABASE_URL` | — | Point at a **read-only** role |
| `APERTURE_BEDROCK_MODEL_ID` | `qwen.qwen3-coder-next` | Any Bedrock Converse model |
| `APERTURE_BEDROCK_REGION` | `us-east-1` | Model ids are region-scoped |
| `APERTURE_MAX_REPAIR_ATTEMPTS` | `2` | |
| `APERTURE_ROW_LIMIT` | `1000` | Injected when a query has no `LIMIT` |
| `APERTURE_BUDGET_CEILING_USD` | `10.0` | Hard stop, cumulative across restarts |
| `APERTURE_PII_COLUMN_PATTERN` | email/phone/… | Values for matching columns never enter a prompt |

There is deliberately **no write credential**: write statements are refused, not escalated.

## Semantic layer

`backend/aperture/semantic/semantic.yaml` holds the definitions a schema cannot supply — what counts
as revenue, which statuses are a sale, that money is stored in paise. Only the definitions a question
touches enter the prompt. Generate a starter for your own database from its schema and profile with
`aperture.semantic.write_skeleton`.

## Evaluation

```bash
python benchmarks/bird.py --limit 20     # smoke run
python benchmarks/bird.py --limit 500    # full Mini-Dev
```

Execution accuracy against BIRD Mini-Dev's SQLite split: gold and predicted SQL are both executed
and their result sets compared as multisets, so a differently-written correct query still counts.
Running the benchmark through SQLite exercises the same `Database`, validator and graph as the
PostgreSQL demo.

## Tests

```bash
cd backend && PYTHONPATH= pytest      # 55 tests, no database required
```

`PYTHONPATH=` is only needed if your shell exports one that shadows the venv.

## Privacy note

Query results flow to the configured model provider, and to LangSmith if tracing is enabled. Columns
matching `APERTURE_PII_COLUMN_PATTERN` have their observed values withheld from prompts, but returned
rows are not redacted. Point Aperture at data you are permitted to send.

## Licence

MIT.
